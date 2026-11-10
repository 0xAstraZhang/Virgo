"""Scrollable, event-driven conversation view for the fullscreen client.

Only the Textual thread touches these widgets.  Agent events delivered here are
already detached snapshots; a stream updates one message, never one row per token.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, Static

from ...agent.types import AgentEvent
from ...ai.types import (
    AssistantMessage,
    ImageContent,
    ThinkingContent,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
)
from ..console import _brief


class FoldHeader(Static, can_focus=True):
    """A keyboard- and mouse-accessible fold handle without a button frame."""

    BINDINGS = [("enter,space", "toggle", "Toggle details")]
    DEFAULT_CSS = """
    FoldHeader { height: auto; min-height: 1; background: $background; }
    FoldHeader:focus { text-style: reverse; }
    """

    class Toggled(Message):
        def __init__(self, header: FoldHeader) -> None:
            self.header = header
            super().__init__()

    def action_toggle(self) -> None:
        self.post_message(self.Toggled(self))

    def on_click(self) -> None:
        self.action_toggle()


class AssistantView(Vertical):
    DEFAULT_CSS = """
    AssistantView { height: auto; margin-bottom: 1; background: $background; }
    AssistantView > .speaker { height: 1; text-style: bold; color: $foreground; }
    AssistantView > .thought-header {
        height: 1; color: $foreground; text-style: dim;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    AssistantView > .thought-header.active { color: $accent; text-style: none; }
    AssistantView > .thought-body { height: auto; text-style: dim; padding-left: 2; }
    AssistantView > Markdown { margin: 0; padding: 0; background: $background; }
    AssistantView MarkdownFence, AssistantView MarkdownFence > Label {
        background: $background; color: $foreground;
    }
    AssistantView > .error { height: auto; color: $error; }
    AssistantView > .aborted { color: $warning; }
    """

    def __init__(self, agent: str, *, show_thinking: bool = False) -> None:
        super().__init__()
        self.agent = agent
        self.show_thinking = show_thinking
        self.text = ""
        self.thinking = ""
        self.message: AssistantMessage | None = None
        self.activity: Text | None = None
        self._stream = None
        self.speaker = Static(agent, classes="speaker", markup=False)
        self.thought_header = FoldHeader("", classes="thought-header", markup=False)
        self.thought_body = Static("", classes="thought-body", markup=False)
        self.body = Markdown(open_links=False)
        self.error = Static("", classes="error", markup=False)

    def compose(self) -> ComposeResult:
        if self.agent != "main":
            yield self.speaker
        yield self.thought_header
        yield self.thought_body
        yield self.body
        yield self.error

    def on_mount(self) -> None:
        self.thought_header.display = False
        self.thought_body.display = False
        self.body.display = False
        self.error.display = False

    async def _stop_stream(self) -> None:
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

    async def on_unmount(self) -> None:
        await self._stop_stream()

    async def set_message(self, message: AssistantMessage, *, final: bool = False) -> None:
        self.message = message
        text = message.text()
        self.thinking = "\n\n".join(
            "[redacted thinking]" if block.redacted else block.thinking
            for block in message.content
            if isinstance(block, ThinkingContent)
        )
        self._render_thinking()
        if final:
            await self._stop_stream()
            # A final message is authoritative, including errors and truncated text.
            if text != self.text or self.body._markdown != text:
                await self.body.update(text)
        elif text.startswith(self.text):
            delta = text[len(self.text) :]
            if delta:
                if self._stream is None:
                    self._stream = Markdown.get_stream(self.body)
                await self._stream.write(delta)
        else:
            await self._stop_stream()
            await self.body.update(text)
        self.text = text
        self.body.display = bool(text)
        error = ""
        if final and message.stop_reason in ("error", "refusal", "aborted", "length"):
            error = (
                message.error_message
                or {
                    "error": "The model request failed.",
                    "refusal": "The model declined this request.",
                    "aborted": "Interrupted.",
                    "length": "The response reached its output limit.",
                }[message.stop_reason]
            )
        self.error.update(error)
        self.error.display = bool(error)
        self.error.set_class(message.stop_reason == "aborted", "aborted")
        # Tool-only assistant messages are represented by their tool cards.
        self.display = bool(text or self.thinking or error or self.activity or not final)

    async def reset_attempt(self) -> None:
        await self._stop_stream()
        self.text = ""
        self.thinking = ""
        self.message = None
        await self.body.update("")
        self.body.display = False
        self.error.display = False
        self._render_thinking()

    def _render_thinking(self) -> None:
        self.thought_header.display = bool(self.thinking or self.activity)
        title = Text(f"{'▾' if self.show_thinking else '▸'} ", no_wrap=True, overflow="ellipsis")
        title.append(self.activity if self.activity is not None else "Thought")
        self.thought_header.update(title)
        self.thought_header.set_class(self.activity is not None, "active")
        self.thought_body.display = bool(self.thinking and self.show_thinking)
        if self.thought_body.display:
            self.thought_body.update(self.thinking)

    def set_activity(self, label: Text | None) -> None:
        if label == self.activity:
            return
        self.activity = label.copy() if label is not None else None
        self._render_thinking()
        self.display = bool(self.text or self.thinking or self.error.display or self.activity)

    def set_thinking(self, expanded: bool) -> None:
        self.show_thinking = expanded
        self._render_thinking()

    @on(FoldHeader.Toggled)
    def toggle_thought(self, event: FoldHeader.Toggled) -> None:
        event.stop()
        self.set_thinking(not self.show_thinking)


class ToolView(Vertical):
    DEFAULT_CSS = """
    ToolView { height: auto; margin-bottom: 1; background: $background; }
    ToolView > FoldHeader { color: $accent; }
    ToolView > Static.output { height: auto; padding-left: 2; text-style: dim; }
    ToolView.error > FoldHeader, ToolView.error > Static.output { color: $error; }
    """

    def __init__(
        self, agent: str, call_id: str, name: str, args: dict | None = None, *, expanded: bool = False
    ) -> None:
        super().__init__()
        self.agent = agent
        self.call_id = call_id
        self.tool_name = name
        self.args = args or {}
        self.expanded = expanded
        self.status = "pending"
        self.result = ""
        self.duration: float | None = None
        self.header = FoldHeader("", markup=False)
        self.output = Static("", classes="output", markup=False)

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.output

    def on_mount(self) -> None:
        self.render_state()

    def render_state(self) -> None:
        who = "" if self.agent == "main" else f"[{self.agent}] "
        marker = {"pending": "○", "running": "●", "done": "✓", "error": "✗"}[self.status]
        duration = f" · {self.duration:g}s" if self.duration is not None else ""
        title = f"{'▾' if self.expanded else '▸'} {marker} {who}{self.tool_name}({_brief(self.tool_name, self.args)}){duration}"
        self.header.update(title)
        lines = self.result.splitlines()
        if self.expanded:
            preview = self.result
        else:
            preview = "\n".join(lines[:3])
            if len(lines) > 3:
                preview += f"\n… {len(lines) - 3} more lines · Enter/click to expand"
        if not preview:
            preview = (
                "Running…"
                if self.status == "running"
                else "(no output)"
                if self.status in ("done", "error")
                else ""
            )
        self.output.update(preview)
        self.output.display = bool(preview)
        self.set_class(self.status == "error", "error")

    def set_result(self, text: str, *, is_error: bool = False, details=None) -> None:
        self.result = text
        self.status = "error" if is_error else "done"
        value = details.get("duration") if isinstance(details, dict) else None
        self.duration = value if isinstance(value, (int, float)) else None
        self.render_state()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.render_state()

    @on(FoldHeader.Toggled)
    def toggle_output(self, event: FoldHeader.Toggled) -> None:
        event.stop()
        self.set_expanded(not self.expanded)


class Transcript(VerticalScroll, inherit_bindings=False):
    """Conversation widgets with native Textual bottom anchoring.

    ``follow_latest`` is released by wheel, scrollbar or keyboard navigation and
    automatically resumes when the user reaches the bottom. ``go_latest`` also
    restores it explicitly. The application can use it to show a jump button.
    """

    # Jumping to the latest message is a click-only action.
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
        self.show_tools = False
        self.show_thinking = False
        self.active: dict[str, AssistantView] = {}
        self.activity_card: AssistantView | None = None
        self.tools: dict[tuple[str, str], ToolView] = {}

    @property
    def follow_latest(self) -> bool:
        return self._anchored and not self._anchor_released

    @follow_latest.setter
    def follow_latest(self, value: bool) -> None:
        if value:
            self.go_latest()
        else:
            self.release_anchor()

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

    async def _assistant(self, agent: str) -> AssistantView:
        card = self.active.get(agent)
        if card is None:
            card = AssistantView(agent, show_thinking=self.show_thinking)
            self.active[agent] = card
            await self.mount(card)
        if agent == "main" and self.activity_card is not card:
            if self.activity_card is not None:
                self.activity_card.set_activity(None)
            self.activity_card = card
        return card

    def show_activity(self, label: Text | None) -> None:
        if self.activity_card is not None:
            self.activity_card.set_activity(label)

    def finish_activity(self) -> None:
        self.show_activity(None)
        self.activity_card = None
        self.active.pop("main", None)

    async def _tool(self, agent: str, call_id: str, name: str, args: dict | None = None) -> ToolView:
        key = (agent, call_id)
        card = self.tools.get(key)
        if card is None:
            card = ToolView(agent, call_id, name, args, expanded=self.show_tools)
            self.tools[key] = card
            await self.mount(card)
        else:
            card.tool_name = name or card.tool_name
            if args is not None:
                card.args = args
        return card

    async def consume(self, ev: AgentEvent) -> None:
        message = ev.message
        if ev.type == "turn_start" and ev.agent == "main":
            # This card also carries feedback while waiting for the first token.
            # Prompts emitted immediately afterwards are inserted before it.
            await self._assistant(ev.agent)
        elif ev.type == "message_start" and isinstance(message, AssistantMessage):
            card = await self._assistant(ev.agent)
            await card.set_message(message)
        elif ev.type == "message_update" and ev.stream_event is not None:
            card = await self._assistant(ev.agent)
            if ev.stream_event.type == "retry":
                await card.reset_attempt()
            else:
                await card.set_message(ev.stream_event.partial)
        elif ev.type == "message_end":
            if isinstance(message, UserMessage):
                labels = {
                    "notification": "Notification",
                    "summary": "Context summary",
                    "continue": "Continuation",
                }
                speaker = labels.get(message.kind, "")
                if ev.agent != "main":
                    speaker = f"{speaker} · {ev.agent}" if speaker else ev.agent
                text = Text("> ")
                if speaker:
                    text.append(speaker + "\n")
                text.append(message.text())
                images = (
                    []
                    if isinstance(message.content, str)
                    else [b for b in message.content if isinstance(b, ImageContent)]
                )
                for image in images:
                    text.append(f"\n[image: {image.mime_type}]", style="dim")
                pending = self.active.get(ev.agent)
                before = pending if pending is not None and pending.message is None else None
                await self.mount(Static(text, classes="user-message"), before=before)
            elif isinstance(message, AssistantMessage):
                card = await self._assistant(ev.agent)
                await card.set_message(message, final=True)
                self.active.pop(ev.agent, None)
                for call in message.tool_calls():
                    await self._tool(ev.agent, call.id, call.name, call.arguments)
            elif isinstance(message, ToolResultMessage):
                card = await self._tool(ev.agent, message.tool_call_id, message.tool_name)
                text = message.text()
                for image in message.content:
                    if isinstance(image, ImageContent):
                        text += f"\n[image: {image.mime_type}]"
                card.set_result(text, is_error=message.is_error, details=message.details)
        elif ev.type == "tool_execution_start" and ev.tool_call_id:
            card = await self._tool(ev.agent, ev.tool_call_id, ev.tool_name or "tool", ev.args)
            card.status = "running"
            card.render_state()
        elif ev.type == "tool_execution_end" and ev.tool_call_id:
            card = await self._tool(ev.agent, ev.tool_call_id, ev.tool_name or "tool", ev.args)
            card.set_result(
                ev.result.as_text() if ev.result else "",
                is_error=ev.is_error,
                details=ev.result.details if ev.result else None,
            )

    async def load_history(self, entries: list[dict]) -> None:
        for card in self.query(AssistantView):
            await card._stop_stream()
        self.active.clear()
        self.activity_card = None
        self.tools.clear()
        await self.remove_children()
        for entry in entries:
            if entry.get("type") == "message":
                try:
                    message = message_from_dict(entry["message"])
                except (KeyError, TypeError, ValueError):
                    await self.add_notice("Unable to display a saved message.", "warn")
                    continue
                await self.consume(AgentEvent("message_end", message=message))
            elif entry.get("type") == "compaction":
                await self.add_notice("Earlier context compacted · full conversation retained", "dim")
        # Restored answers are never activity targets for subsequent local commands.
        self.activity_card = None
        self.go_latest()

    async def add_notice(self, text: str, style: str | None = None) -> None:
        rich_style = {"error": "red", "warn": "yellow", "dim": "dim", "ok": "green", "bold": "bold"}.get(
            style, ""
        )
        await self.mount(Static(Text(text, style=rich_style), classes="notice"))

    def toggle_tools(self) -> None:
        self.show_tools = not self.show_tools
        for card in self.tools.values():
            card.set_expanded(self.show_tools)

    def toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        for card in self.query(AssistantView):
            card.set_thinking(self.show_thinking)
