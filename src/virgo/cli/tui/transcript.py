"""Chronological messages with independently folded reasoning and tools."""

from __future__ import annotations

import asyncio
import json
from functools import partial

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Size
from textual.layout import DockArrangeResult
from textual.widget import Widget
from textual.widgets import Markdown, Static

from ...agent.types import AgentEvent
from ...ai.stream import StreamEvent
from ...ai.types import (
    AssistantMessage,
    ImageContent,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    block_from_dict,
    message_from_dict,
)
from ...ai.web import native_event
from .folding import Foldable, FoldHeader

# Saved runs replayed at a time: when a session opens, then whenever the
# reader scrolls within OLDER_MARGIN screens of the first replayed run.
HISTORY_RUNS = 30
OLDER_MARGIN = 2
TOOL_KINDS = ("tool_call", "tool_result", "sources")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _same_content(a, b) -> bool:
    if type(a) is not type(b) or a != b:
        return False
    if isinstance(a, Text):  # Text equality compares only plain text and spans
        return (a.style, a.justify, a.overflow, a.no_wrap) == (b.style, b.justify, b.overflow, b.no_wrap)
    return True


def update_static(widget: Static, content, *, layout: bool = True) -> None:
    """Skip no-op updates: ``Static.update`` always refreshes, and a layout refresh
    re-arranges every widget on screen, so its cost grows with the transcript."""
    if not _same_content(widget.content, content):
        widget.update(content, layout=layout)


def _is_run_start(entry: dict) -> bool:
    return entry.get("type") == "custom" and entry.get("kind") == "transcript_run_start"


def _split_runs(entries: list[dict]) -> list[list[dict]]:
    """Split at run starts, where replay state resets, so each part stands alone."""
    runs: list[list[dict]] = [[]]
    for entry in entries:
        if _is_run_start(entry) and runs[-1]:
            runs.append([])
        runs[-1].append(entry)
    return runs


class EventView(Foldable):
    """One logical event: a single summary line and opt-in full content."""

    DEFAULT_CSS = """
    EventView { height: auto; margin: 0; background: $background; }
    EventView > FoldHeader { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    EventView > .output { height: auto; padding-left: 2; text-style: dim; }
    EventView.message > .output { padding: 0; margin: 1 0; text-style: none; }
    EventView.tool > FoldHeader { color: $accent; }
    EventView.error > FoldHeader, EventView.error > .output { color: $error; }
    """

    def __init__(self, kind: str, text: str = "", *, agent: str = "main", name: str = "") -> None:
        super().__init__()
        self.kind, self.agent, self.tool_name = kind, agent, name
        self.text = text
        self.expanded = False
        self.header = FoldHeader("", markup=False)
        self.output = (Markdown(text, classes="output", open_links=False) if kind == "text"
                       else Static("", classes="output", markup=False))
        self._rendered_text = text
        self.set_class(kind == "text", "message")
        self.set_class(kind in ("tool_call", "tool_result"), "tool")

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.output

    def on_mount(self) -> None:
        self.render_state()

    def on_resize(self) -> None:
        self.render_state()

    def summary(self) -> Text:
        title = Text("▾ " if self.expanded else "▸ ", no_wrap=True, overflow="ellipsis")
        if self.agent != "main":
            title.append(f"[{self.agent}] ")
        if self.tool_name:
            title.append(self.tool_name, style="bold")
        else:
            title.append({"thinking": "Thought", "text": "Message", "sources": "Sources"}.get(
                self.kind, self.kind.title()
            ))
        if not (self.expanded and self.kind in TOOL_KINDS):
            title.append(" → " if self.tool_name and self.kind == "tool_result" else " · ")
            title.append(" ".join(self.text.split()) or "…")
        return title

    def render_state(self) -> None:
        if self.kind == "text":
            self.header.display = self.agent != "main"
            update_static(self.header, Text(f"[{self.agent}]"), layout=False)
            self.output.display = True
            if self._rendered_text != self.text:
                self._rendered_text = self.text
                self.output.update(self.text)
            return
        if self.kind == "thinking":
            self.display = bool(self.text.strip())
        title = self.summary()
        width = self.header.size.width
        if width and title.cell_len > width:
            title.truncate(max(0, width - 3), overflow="crop")
            title.append("." * min(3, width))
        # Headers are one fixed line, so new text never needs a layout pass.
        update_static(self.header, title, layout=False)
        update_static(self.output, self.text or "(no output)")
        self.output.display = self.expanded

    def set_text(self, text: str) -> None:
        self.text = text
        self.render_state()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.render_state()

    def action_toggle(self) -> None:
        if self.kind != "text":
            self.set_expanded(not self.expanded)

    def _owns_fold_gesture(self, widget) -> bool:
        return self.kind != "text" and super()._owns_fold_gesture(widget)


class ToolView(EventView):
    """A tool call or its separate result; arrival never moves the call."""

    def __init__(self, agent: str, call_id: str, name: str, args: dict | None = None, *, result=False):
        super().__init__("tool_result" if result else "tool_call", agent=agent, name=name)
        self.call_id, self.args = call_id, args or {}
        self.status = "done" if result else "pending"
        self.result = ""
        self.duration: float | None = None
        if not result:
            self.text = _json(self.args)

    def summary(self) -> Text:
        title = super().summary()
        if self.duration is not None:
            title.append(f" · {self.duration:g}s")
        return title

    def set_result(self, text: str, *, is_error: bool = False, details=None) -> None:
        self.result = text
        self.status = "error" if is_error else "done"
        value = details.get("duration") if isinstance(details, dict) else None
        if isinstance(value, (int, float)):
            self.duration = value
        self.set_class(is_error, "error")
        self.set_text(text)


class AssistantView(Vertical):
    """An always-visible run timeline, followed by the current final answer."""

    DEFAULT_CSS = """
    AssistantView { height: auto; margin-bottom: 1; background: $background; }
    AssistantView > .thought-header {
        height: 1; color: $foreground; text-style: dim;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    AssistantView > .thought-header.active { color: $accent; text-style: none; }
    AssistantView > .thought-body { height: auto; }
    AssistantView > Markdown { margin: 0; padding: 0; background: $background; }
    AssistantView MarkdownFence, AssistantView MarkdownFence > Label {
        background: $background; color: $foreground;
    }
    AssistantView > .error { height: auto; color: $error; }
    AssistantView > .aborted { color: $warning; }
    """

    def __init__(self, agent: str = "main", *, show_thinking: bool = False) -> None:
        super().__init__()
        self.agent, self.show_thinking = agent, show_thinking
        self.text, self.thinking = "", ""
        self.message: AssistantMessage | None = None
        self.activity: Text | None = None
        self.events: list[EventView] = []
        self.answer_events: list[EventView] = []
        self.thought_header = Static("", classes="thought-header", markup=False)
        self.thought_body = Vertical(classes="thought-body")
        self.body = Markdown(open_links=False)
        self.error = Static("", classes="error", markup=False)
        self.body.display = self.error.display = False
        self.has_prompt = False

    def compose(self) -> ComposeResult:
        # A replayed run is built before it is mounted; compose its events in
        # one pass. Live runs are mounted empty and receive events afterwards.
        yield self.thought_header
        with self.thought_body:
            yield from self.events
        yield self.body
        yield self.error

    def on_mount(self) -> None:
        self._render_thinking()

    async def append_event(self, event: EventView) -> None:
        self.events.append(event)
        if event.kind == "thinking":
            event.set_expanded(self.show_thinking)
        if self.thought_body.is_attached:
            await self.thought_body.mount(event)
        self._render_thinking()

    async def _show_answer(self, text: str) -> None:
        # Each Markdown update parses in a thread and remounts its blocks, so
        # skip no-op updates (every message start clears the answer).
        if text != self.text:
            self.text = text
            if self.body.is_attached:
                await self.body.update(text)
            else:
                # Not mounted yet (history replay): render once, on mount.
                self.body = Markdown(text, open_links=False)
        self.body.display = bool(text)

    async def clear_answer(self) -> None:
        for event in self.answer_events:
            event.display = True
        self.answer_events.clear()
        await self._show_answer("")
        self.error.display = False
        self._render_thinking()

    async def set_answer(self, message: AssistantMessage, events: list[EventView]) -> None:
        self.message, self.answer_events = message, events
        for event in events:
            event.display = False
        await self._show_answer("".join(event.text for event in events))
        error = ""
        if message.stop_reason in ("error", "refusal", "aborted", "length"):
            error = message.error_message or {
                "error": "The model request failed.", "refusal": "The model declined this request.",
                "aborted": "Interrupted.", "length": "The response reached its output limit.",
            }[message.stop_reason]
        self.error.update(error)
        self.error.display = bool(error)
        self.error.set_class(message.stop_reason == "aborted", "aborted")
        self._render_thinking()

    def _render_thinking(self) -> None:
        self.thought_header.display = self.activity is not None
        update_static(self.thought_header, self.activity or Text(""), layout=False)
        self.thought_header.set_class(self.activity is not None, "active")
        self.thought_body.display = True

    def set_activity(self, label: Text | None) -> None:
        self.activity = label.copy() if label is not None else None
        self._render_thinking()

    def set_thinking(self, expanded: bool) -> None:
        self.show_thinking = expanded
        for event in self.events:
            if event.kind == "thinking":
                event.set_expanded(expanded)


class Transcript(VerticalScroll, inherit_bindings=False):
    """Append in arrival order, updating streams in their original slots."""

    BINDINGS = [binding for binding in VerticalScroll.BINDINGS if binding.action != "scroll_end"]
    DEFAULT_CSS = """
    Transcript {
        height: 1fr; width: 1fr; padding: 0 1;
        background: $background; color: $foreground;
        scrollbar-background: $background; scrollbar-corner-color: $background;
    }
    Transcript > .user-message {
        height: auto; margin-bottom: 1; padding: 0 1;
        background: #EEEEEE; color: #000000;
    }
    Transcript > .notice { height: auto; margin-bottom: 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.show_tools = self.show_thinking = False
        self.active: dict[str, AssistantView] = {}
        self.activity_card: AssistantView | None = None
        self.tools: dict[tuple[str, str], ToolView] = {}
        self.results: dict[tuple[str, str], ToolView] = {}
        self._current_calls: dict[tuple[str, str], ToolView] = {}
        self._current_results: dict[tuple[str, str], ToolView] = {}
        self._blocks: dict[str, dict[int, EventView]] = {}
        self._native_results: dict[tuple[str, str], ToolView] = {}
        self._message_open: set[str] = set()
        self._run: AssistantView | None = None
        self._running = False
        # Top-level widgets built by history replay, mounted together at the end.
        self._staged: list[Widget] | None = None
        # Saved runs not replayed yet (oldest first) and how to show them.
        self._older: list[list[dict]] = []
        self._older_lock = asyncio.Lock()
        self._older_pending = False
        self._older_tools = False
        # While older runs are prepended: a widget below them, its last y, and
        # whether the mount is still under way.
        self._keep_in_view: tuple[Widget, int] | None = None
        self._prepending = False

    @property
    def follow_latest(self) -> bool:
        return self._anchored and not self._anchor_released

    @follow_latest.setter
    def follow_latest(self, value: bool) -> None:
        self.go_latest() if value else self.release_anchor()

    def on_mount(self) -> None:
        self.anchor()

    def go_latest(self) -> None:
        # Anchor scrolls immediately and follows subsequent layout changes.
        # A separate deferred scroll_end would override later manual scrolling.
        self.anchor()

    def action_page_up(self) -> None:
        self.release_anchor()
        self._scroll_page(-1)

    def action_page_down(self) -> None:
        self._scroll_page(1)

    def _scroll_page(self, direction: int) -> None:
        # Scroll now: Textual otherwise applies an absolute target after the
        # next refresh, which would be stale once older runs are prepended.
        y = self.scroll_y + direction * self.scrollable_content_region.height
        self.scroll_to(y=y, animate=False, immediate=True)

    def action_scroll_home(self) -> None:
        self.release_anchor()
        if self._older:
            # The top is the session's first run: replay the rest, then go there.
            self.run_worker(partial(self._load_older, everything=True), group="older-history")
        else:
            self.scroll_home(animate=False, x_axis=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._check_older()

    def watch_virtual_size(self) -> None:
        self._check_older()

    def _check_older(self) -> None:
        height = self.size.height
        if self._older and not self._older_pending and height and self.scroll_y < OLDER_MARGIN * height:
            self._older_pending = True
            # Pass the method, not a coroutine: a worker cancelled before it
            # starts would otherwise leave that coroutine never awaited.
            self.run_worker(self._load_older, group="older-history")

    async def _load_older(self, *, everything: bool = False) -> None:
        try:
            async with self._older_lock:
                if self._older:
                    count = len(self._older) if everything else HISTORY_RUNS
                    runs, self._older = self._older[-count:], self._older[:-count]
                    widgets = await self._replay_detached([entry for run in runs for entry in run], True)
                    with self.app.batch_update():
                        first = self.children[0] if self.children else None
                        if first is not None and not everything:
                            # Prepending moves everything down; keep the reader's place.
                            self._keep_in_view = (first, first.virtual_region.y)
                        self._prepending = True
                        try:
                            await self.mount_all(widgets, before=first)
                        finally:
                            self._prepending = False
        finally:
            self._older_pending = False
        if everything:
            self.scroll_home(animate=False, x_axis=False)
        else:
            self.call_after_refresh(self._check_older)

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        arrangement = super().arrange(size, optimal)
        if self._keep_in_view is not None and not optimal:
            # The compositor scrolls right after arranging, so moving the
            # scroll position by however far the prepended runs pushed this
            # widget keeps even the first frame in place. (Textual applies its
            # own bottom anchor at the same point.)
            widget, old_y = self._keep_in_view
            new_y = next((p.region.y for p in arrangement.placements if p.widget is widget), old_y)
            # Follow it through any reflow during the mount, then stop. Update
            # this first: moving the scrollbar below can re-enter arrange.
            self._keep_in_view = (widget, new_y) if self._prepending else None
            if new_y != old_y:
                y = self.scroll_y + new_y - old_y
                self.set_reactive(Widget.scroll_y, y)
                self.set_reactive(Widget.scroll_target_y, y)
                if self.show_vertical_scrollbar:
                    self.vertical_scrollbar.position = y
        return arrangement

    async def _assistant(self, agent: str = "main") -> AssistantView:
        if self._run is None:
            self._run = AssistantView(show_thinking=self.show_thinking)
            self.active[agent] = self.activity_card = self._run
            await self._place(self._run)
        self.active[agent] = self.activity_card = self._run
        return self._run

    async def _place(self, widget: Widget, *, before: Widget | None = None) -> None:
        if self._staged is None:
            await self.mount(widget, before=before)
        elif before is None:
            self._staged.append(widget)
        else:
            self._staged.insert(self._staged.index(before), widget)

    def show_activity(self, label: Text | None) -> None:
        if self.activity_card is not None:
            self.activity_card.set_activity(label)

    def finish_activity(self) -> None:
        self.show_activity(None)
        self.activity_card = self._run = None
        self.active.clear()
        self._blocks.clear()
        self._current_calls.clear()
        self._current_results.clear()
        self._native_results.clear()
        self._message_open.clear()
        self._running = False

    async def _append(self, row: EventView) -> EventView:
        card = await self._assistant(row.agent)
        await card.append_event(row)
        return row

    async def _tool(self, agent: str, call_id: str, name: str, args: dict | None = None) -> ToolView:
        key = (agent, call_id)
        card = self._current_calls.get(key)
        if card is None:
            card = ToolView(agent, call_id, name, args)
            self.tools[key] = self._current_calls[key] = card
            await self._append(card)
        else:
            card.tool_name = name or card.tool_name
            if args is not None:
                card.args = args
                card.set_text(_json(args))
        return card

    async def _result(self, agent: str, message: ToolResultMessage) -> ToolView:
        key = (agent, message.tool_call_id)
        row = self._current_results.get(key)
        if row is None:
            row = ToolView(agent, message.tool_call_id, message.tool_name, result=True)
            self.results[key] = self._current_results[key] = row
            await self._append(row)
        text = message.text()
        for block in message.content:
            if isinstance(block, ImageContent):
                text += f"\n[image: {block.mime_type}]"
        row.set_result(text, is_error=message.is_error, details=message.details)
        if call := self._current_calls.get(key):
            call.status = row.status
        return row

    async def _start_message(self, agent: str) -> AssistantView:
        card = await self._assistant(agent)
        if agent == "main":
            await card.clear_answer()
        # IDs belong to one model message. Providers may reuse them later in
        # the same run; only lifecycle duplicates of this occurrence coalesce.
        for lookup in (self._current_calls, self._current_results, self._native_results):
            for key in list(lookup):
                if key[0] == agent:
                    del lookup[key]
        self._blocks[agent] = {}
        self._message_open.add(agent)
        return card

    async def _snapshot(self, agent: str, message: AssistantMessage, *, final=False) -> None:
        card = await self._assistant(agent)
        if agent not in self._message_open:
            await self._start_message(agent)
        blocks = self._blocks[agent]
        # Bridge coalescing can skip token updates; scan all newly added blocks.
        for index, block in enumerate(message.content):
            row = blocks.get(index)
            if isinstance(block, ToolCall):
                row = await self._tool(agent, block.id, block.name, block.arguments)
            elif isinstance(block, (TextContent, ThinkingContent)):
                if isinstance(block, ThinkingContent):
                    kind = "thinking"
                    # Opaque signatures remain in the message for model replay.
                    # Only readable, non-redacted reasoning belongs in the UI.
                    value = "" if block.redacted else block.thinking
                else:
                    kind = "sources" if block.text.startswith("\n\nSources:\n") else "text"
                    value = block.text
                if row is None:
                    row = await self._append(EventView(kind, agent=agent))
                row.set_text(value)
            elif isinstance(block, RawContent):
                row = await self._native(agent, block, row, index)
            elif isinstance(block, ImageContent) and row is None:
                row = await self._append(EventView("image", f"[image: {block.mime_type}]", agent=agent))
            if row is not None:
                blocks[index] = row
        card.thinking = "\n\n".join(e.text for e in card.events if e.kind == "thinking")
        card.message = message
        if final:
            terminal_error = message.stop_reason in ("error", "refusal", "aborted", "length")
            if agent == "main" and (terminal_error or (not message.tool_calls() and message.stop_reason != "pause_turn")):
                last_tool = max((i for i, row in blocks.items() if row.kind in ("tool_call", "tool_result")), default=-1)
                text_rows = [(i, row) for i, row in blocks.items() if row.kind == "text"]
                answer = [row for i, row in text_rows if i > last_tool]
                # Native search can append its final answer to a text block
                # opened before the search. Block order alone must not hide
                # all available text when a terminal message has no suffix.
                if not any(row.text.strip() for row in answer):
                    answer = [row for _, row in text_rows]
                await card.set_answer(message, answer)
            self._message_open.discard(agent)

    async def _native(self, agent: str, block: RawContent, row: EventView | None, index: int):
        data, kind = block.data, block.data.get("type", "")
        if kind in ("server_tool_use", "web_search_call"):
            info = native_event(data)
            name, call_id = info["name"], data.get("id") or f"native-{index}"
            args = data.get("input", {}) if kind == "server_tool_use" else data.get("action", {})
            if row is None:
                row = await self._append(ToolView(agent, call_id, name, args))
            row.tool_name = name
            row.set_text(_json(args))
            if kind == "web_search_call" and data.get("status") in ("completed", "failed", "incomplete"):
                key = (agent, call_id)
                result = self._native_results.get(key)
                if result is None:
                    result = ToolView(agent, call_id, name, result=True)
                    self._native_results[key] = result
                    await self._append(result)
                result.set_result(_json(args), is_error=data.get("status") != "completed")
        elif kind.endswith("_tool_result"):
            if row is None:
                row = await self._append(ToolView(agent, data.get("tool_use_id", ""), kind.removesuffix("_tool_result"), result=True))
            info = native_event(data)
            content = info["result"] if info else data.get("content", {})
            row.set_result(_json(content), is_error=isinstance(content, dict) and "error" in content.get("type", ""))
        elif row is None:
            row = await self._append(EventView("context", _json(data), agent=agent))
        return row

    async def consume(self, ev: AgentEvent) -> None:
        message = ev.message
        if ev.type == "agent_start" and ev.agent == "main":
            self.finish_activity()
            self._running = True
        elif ev.type == "turn_start" and ev.agent == "main":
            await self._assistant(ev.agent)
        elif ev.type == "message_start" and isinstance(message, AssistantMessage):
            await self._start_message(ev.agent)
            await self._snapshot(ev.agent, message)
        elif ev.type == "message_update" and ev.stream_event is not None:
            if ev.stream_event.type == "retry":
                await self._append(EventView("retry", "Retrying model request", agent=ev.agent))
                await self._start_message(ev.agent)
            else:
                await self._snapshot(ev.agent, ev.stream_event.partial)
        elif ev.type == "message_end":
            if isinstance(message, UserMessage):
                await self._user_message(ev.agent, message)
            elif isinstance(message, AssistantMessage):
                await self._snapshot(ev.agent, message, final=True)
            elif isinstance(message, ToolResultMessage):
                await self._result(ev.agent, message)
        elif ev.type == "tool_execution_start" and ev.tool_call_id:
            row = await self._tool(ev.agent, ev.tool_call_id, ev.tool_name or "tool", ev.args)
            row.status = "running"
        elif ev.type == "tool_execution_end" and ev.tool_call_id:
            await self._result(ev.agent, ToolResultMessage(
                ev.tool_call_id, ev.tool_name or "tool", ev.result.content if ev.result else [],
                details=ev.result.details if ev.result else None, is_error=ev.is_error,
            ))

    async def _user_message(self, agent: str, message: UserMessage) -> None:
        text = message.text()
        if isinstance(message.content, list):
            for image in message.content:
                if isinstance(image, ImageContent):
                    text += f"\n[image: {image.mime_type}]"
        if agent != "main" or message.kind or (self._running and self._run and self._run.has_prompt):
            await self._append(EventView(message.kind or "user", text, agent=agent))
            return
        pending = self._run
        if pending is not None and (pending.has_prompt or pending.events):
            self.finish_activity()
            pending = None
        await self._place(Static(Text("> " + text), classes="user-message"), before=pending)
        if pending is not None:
            pending.has_prompt = True

    async def load_history(self, entries: list[dict]) -> None:
        self.workers.cancel_group(self, "older-history")
        self.finish_activity()
        self.tools.clear()
        self.results.clear()
        self._older, self._older_pending, self._older_tools, self._keep_in_view = [], False, False, None
        self._prepending = False
        has_run_boundaries = any(e.get("kind") == "transcript_run_start" for e in entries)
        if has_run_boundaries:
            # Every widget costs Textual a mount and a share of each layout
            # pass, so show the latest runs now and the rest when scrolled to.
            runs = _split_runs(entries)
            self._older = runs[:-HISTORY_RUNS]
            entries = [entry for run in runs[-HISTORY_RUNS:] for entry in run]
        widgets = await self._replay_detached(entries, has_run_boundaries)
        # Mounting replayed widgets one by one laid out the whole transcript
        # after each, so loading grew quadratically. Mount and lay out once.
        with self.app.batch_update():
            await self.remove_children()
            await self.mount_all(widgets)
        self.go_latest()
        # Content that fits on screen can't scroll, so check once it's laid out.
        self.call_after_refresh(self._check_older)

    async def _replay_detached(self, entries: list[dict], has_run_boundaries: bool) -> list[Widget]:
        """Replay saved entries into new top-level widgets that are not mounted yet.

        A separate, never-mounted Transcript keeps its replay state, so a live
        run here is unaffected when older runs are replayed later.
        """
        replay = Transcript()
        replay.show_thinking = self.show_thinking
        replay._staged = []
        await replay._replay(entries, has_run_boundaries)
        replay.finish_activity()
        # Older runs come first, as if the whole history had been replayed.
        self.tools = replay.tools | self.tools
        self.results = replay.results | self.results
        if self._older_tools:
            # Loaded history follows the last toggle, like the runs already shown.
            for card in replay._staged:
                for event in getattr(card, "events", ()):
                    if event.kind in TOOL_KINDS:
                        event.set_expanded(True)
        return replay._staged

    async def _replay(self, entries: list[dict], has_run_boundaries: bool) -> None:
        partials: dict[str, AssistantMessage] = {}
        for entry in entries:
            if entry.get("type") == "message":
                try:
                    message = message_from_dict(entry["message"])
                except (KeyError, TypeError, ValueError):
                    await self.add_notice("Unable to display a saved message.", "warn")
                    continue
                if (
                    not has_run_boundaries and isinstance(message, UserMessage)
                    and message.kind == "notification" and self._run is not None
                    and (self._run.answer_events or self._run.error.display)
                ):
                    # Older stores have no run markers. A notification after a
                    # terminal answer is the best available wake-up boundary.
                    self.finish_activity()
                await self.consume(AgentEvent("message_end", message=message))
                if isinstance(message, AssistantMessage):
                    partials.pop("main", None)
            elif _is_run_start(entry):
                partials.clear()
                await self.consume(AgentEvent("agent_start"))
                await self.consume(AgentEvent("turn_start"))
            elif entry.get("type") == "custom" and entry.get("kind") == "transcript_run_end":
                partials.clear()
                self.finish_activity()
            elif entry.get("type") == "custom" and entry.get("kind") == "transcript_stream":
                data = entry.get("data", {})
                try:
                    agent, kind = data.get("agent", "main"), data["stream_type"]
                    if kind == "retry":
                        partials.pop(agent, None)
                        await self.consume(AgentEvent("message_update", agent=agent, stream_event=StreamEvent(kind, AssistantMessage())))
                        continue
                    index = data["index"]
                    if not isinstance(index, int) or not 0 <= index < 10000:
                        raise ValueError("Invalid saved block index")
                    block = block_from_dict(data["block"])
                    partial = partials.setdefault(agent, AssistantMessage())
                    partial.content.extend([None] * max(0, index + 1 - len(partial.content)))
                    partial.content[index] = block
                    await self.consume(AgentEvent("message_update", agent=agent, stream_event=StreamEvent(kind, partial, index=index)))
                except (KeyError, TypeError, ValueError):
                    await self.add_notice("Unable to display a saved stream event.", "warn")
            elif entry.get("type") == "custom" and entry.get("kind") == "transcript_tool_result":
                data = entry.get("data", {})
                try:
                    message = message_from_dict(data["message"])
                    if isinstance(message, ToolResultMessage):
                        await self._result(data.get("agent", "main"), message)
                except (KeyError, TypeError, ValueError):
                    await self.add_notice("Unable to display a saved tool event.", "warn")
            elif entry.get("type") == "compaction":
                await self.add_notice("Earlier context compacted · full conversation retained", "dim")

    async def add_notice(self, text: str, style: str | None = None) -> None:
        if self._run is not None:
            row = EventView("notice", text)
            row.output.add_class("notice")
            row.set_class(style == "error", "error")
            await self._append(row)
        else:
            rich_style = {"error": "red", "warn": "yellow", "dim": "dim", "ok": "green", "bold": "bold"}.get(style, "")
            await self._place(Static(Text(text, style=rich_style), classes="notice"))

    def toggle_tools(self) -> None:
        self.show_tools = self._older_tools = not self.show_tools
        for card in self.query(EventView):
            if card.kind in TOOL_KINDS:
                card.set_expanded(self.show_tools)

    def toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        for card in self.query(AssistantView):
            card.set_thinking(self.show_thinking)
