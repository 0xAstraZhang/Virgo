"""Chronological, folded run events and final answers for the fullscreen client."""

from __future__ import annotations

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
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
from .folding import Foldable, FoldHeader


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class EventView(Foldable):
    """One logical event: a single summary line and opt-in full content."""

    DEFAULT_CSS = """
    EventView { height: auto; margin: 0; background: $background; }
    EventView > FoldHeader { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    EventView > .output { height: auto; padding-left: 2; text-style: dim; }
    EventView.tool > FoldHeader { color: $accent; }
    EventView.error > FoldHeader, EventView.error > .output { color: $error; }
    """

    def __init__(self, kind: str, text: str = "", *, agent: str = "main", name: str = "") -> None:
        super().__init__()
        self.kind, self.agent, self.tool_name = kind, agent, name
        self.text = text
        self.expanded = False
        self.header = FoldHeader("", markup=False)
        self.output = Static("", classes="output", markup=False)
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
            title.append(" → " if self.kind == "tool_result" else " · ")
        else:
            title.append({"thinking": "Thought", "text": "Message", "sources": "Sources"}.get(
                self.kind, self.kind.title()
            ) + " · ")
        title.append(" ".join(self.text.split()) or "…")
        return title

    def render_state(self) -> None:
        title = self.summary()
        width = self.header.size.width
        if width and title.cell_len > width:
            title.truncate(max(0, width - 3), overflow="crop")
            title.append("." * min(3, width))
        self.header.update(title)
        self.output.update(self.text or "(no output)")
        self.output.display = self.expanded

    def set_text(self, text: str) -> None:
        self.text = text
        self.render_state()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.render_state()

    def action_toggle(self) -> None:
        self.set_expanded(not self.expanded)


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


class AssistantView(Foldable):
    """One Thinking timeline per run, followed by the current final answer."""

    DEFAULT_CSS = """
    AssistantView { height: auto; margin-bottom: 1; background: $background; }
    AssistantView > .thought-header {
        height: 1; color: $foreground; text-style: dim;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    AssistantView > .thought-header.active { color: $accent; text-style: none; }
    AssistantView > .thought-body { height: auto; padding-left: 2; }
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
        self.thought_header = FoldHeader("", classes="thought-header", markup=False)
        self.thought_body = Vertical(classes="thought-body")
        self.body = Markdown(open_links=False)
        self.error = Static("", classes="error", markup=False)
        self.has_prompt = False

    def compose(self) -> ComposeResult:
        yield self.thought_header
        yield self.thought_body
        yield self.body
        yield self.error

    def on_mount(self) -> None:
        self.body.display = self.error.display = False
        self._render_thinking()

    async def append_event(self, event: EventView) -> None:
        self.events.append(event)
        await self.thought_body.mount(event)
        self._render_thinking()

    async def clear_answer(self) -> None:
        for event in self.answer_events:
            event.display = True
        self.answer_events.clear()
        self.text = ""
        await self.body.update("")
        self.body.display = self.error.display = False
        self._render_thinking()

    async def set_answer(self, message: AssistantMessage, events: list[EventView]) -> None:
        self.message, self.answer_events = message, events
        self.text = "".join(event.text for event in events)
        for event in events:
            event.display = False
        await self.body.update(self.text)
        self.body.display = bool(self.text)
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
        count = sum(event.display for event in self.events)
        self.thought_header.display = bool(count or self.activity)
        title = Text("▾ " if self.show_thinking else "▸ ", no_wrap=True, overflow="ellipsis")
        if self.activity is not None:
            if "Thinking" not in self.activity.plain:
                title.append("Thinking · ")
            title.append(self.activity)
        else:
            title.append(f"Thinking · {count} events")
        self.thought_header.update(title)
        self.thought_header.set_class(self.activity is not None, "active")
        self.thought_body.display = bool(self.show_thinking and count)

    def set_activity(self, label: Text | None) -> None:
        self.activity = label.copy() if label is not None else None
        self._render_thinking()

    def set_thinking(self, expanded: bool) -> None:
        self.show_thinking = expanded
        self._render_thinking()

    def action_toggle(self) -> None:
        self.set_thinking(not self.show_thinking)

    def _owns_fold_gesture(self, widget) -> bool:
        # Final answers share the run widget but are outside the Thinking fold.
        return widget in (self, self.thought_header, self.thought_body)


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

    @property
    def follow_latest(self) -> bool:
        return self._anchored and not self._anchor_released

    @follow_latest.setter
    def follow_latest(self, value: bool) -> None:
        self.go_latest() if value else self.release_anchor()

    def on_mount(self) -> None:
        self.anchor()

    def go_latest(self) -> None:
        self.anchor()
        self.scroll_end(animate=False, x_axis=False)

    def action_page_up(self) -> None:
        self.release_anchor()
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_scroll_home(self) -> None:
        self.release_anchor()
        self.scroll_home(animate=False, x_axis=False)

    async def _assistant(self, agent: str = "main") -> AssistantView:
        if self._run is None:
            self._run = AssistantView(show_thinking=self.show_thinking)
            self.active[agent] = self.activity_card = self._run
            await self.mount(self._run)
        self.active[agent] = self.activity_card = self._run
        return self._run

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
                    value = "[redacted thinking]" if block.redacted else block.thinking
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
                await card.set_answer(message, [row for i, row in blocks.items() if i > last_tool and row.kind == "text"])
            self._message_open.discard(agent)

    async def _native(self, agent: str, block: RawContent, row: EventView | None, index: int):
        data, kind = block.data, block.data.get("type", "")
        if kind in ("server_tool_use", "web_search_call"):
            name, call_id = data.get("name") or "web_search", data.get("id") or f"native-{index}"
            args = data.get("input", {}) if kind == "server_tool_use" else data.get("action", {})
            if row is None:
                row = await self._append(ToolView(agent, call_id, name, args))
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
            content = data.get("content", {})
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
        await self.mount(Static(Text("> " + text), classes="user-message"), before=pending)
        if pending is not None:
            pending.has_prompt = True

    async def load_history(self, entries: list[dict]) -> None:
        self.finish_activity()
        self.tools.clear()
        self.results.clear()
        await self.remove_children()
        has_run_boundaries = any(e.get("kind") == "transcript_run_start" for e in entries)
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
            elif entry.get("type") == "custom" and entry.get("kind") == "transcript_run_start":
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
        self.finish_activity()
        self.go_latest()

    async def add_notice(self, text: str, style: str | None = None) -> None:
        if self._run is not None:
            row = EventView("notice", text)
            row.output.add_class("notice")
            row.set_class(style == "error", "error")
            await self._append(row)
        else:
            rich_style = {"error": "red", "warn": "yellow", "dim": "dim", "ok": "green", "bold": "bold"}.get(style, "")
            await self.mount(Static(Text(text, style=rich_style), classes="notice"))

    def toggle_tools(self) -> None:
        self.show_tools = not self.show_tools
        for card in self.query(EventView):
            if card.kind in ("tool_call", "tool_result", "sources"):
                card.set_expanded(self.show_tools)

    def toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        for card in self.query(AssistantView):
            card.set_thinking(self.show_thinking)
