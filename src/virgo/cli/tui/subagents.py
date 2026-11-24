"""Fullscreen sub-agent list picker and read-only trace viewer modals."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...agent.types import AgentEvent
from ..i18n import tr
from .selection import SelectionScreenMixin
from .sessions import _cell, _columns, relative_time
from .transcript import Transcript, update_static


@dataclass(frozen=True)
class SubagentRow:
    title: str
    status: str
    updated: str

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        title_width, status_width, updated_width = _columns(max(1, options.max_width))
        line = _cell("  ○ " + self.title, title_width)
        if status_width:
            line.append("  ")
            line.append_text(_cell(self.status, status_width))
        line.append("  ")
        line.append_text(_cell(self.updated, updated_width, right=True))
        yield line


class SubagentPicker(SelectionScreenMixin, ModalScreen[str | None]):
    """Choose a sub-agent in the current session to inspect its trace."""

    CSS = """
    SubagentPicker {
        align: center middle;
        background: $background;
    }
    SubagentPicker > #subagent-box {
        width: 100%; height: 100%; padding: 1 2;
        background: $background; color: $foreground;
    }
    SubagentPicker #subagent-title {
        height: 1; margin-bottom: 1; color: #7C3AED; text-style: bold;
    }
    SubagentPicker #subagent-list {
        height: 1fr; border: none; padding: 0;
        background: $background; color: $foreground;
    }
    SubagentPicker #subagent-list > .option-list--option-highlighted,
    SubagentPicker #subagent-list:focus > .option-list--option-highlighted {
        background: #7C3AED; color: #FFFFFF; text-style: bold;
    }
    SubagentPicker #subagent-empty {
        height: 1fr; color: $foreground; text-style: dim;
    }
    SubagentPicker #subagent-hints {
        height: 1; margin-top: 1; text-style: dim;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", show=False),
        Binding("up,k", "move(-1)", show=False),
        Binding("down,j", "move(1)", show=False),
        Binding("enter", "choose", show=False),
    ]

    def __init__(self, items: list[dict]) -> None:
        super().__init__()
        self._items = list(items)
        self._ids: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="subagent-box"):
            yield Static(tr(self, "ui.subagent.picker_title"), id="subagent-title", markup=False)
            yield OptionList(id="subagent-list")
            yield Static(tr(self, "features.agent.list_empty"), id="subagent-empty", markup=False)
            yield Static(tr(self, "ui.subagent.picker_hints"), id="subagent-hints", markup=False)

    def on_mount(self) -> None:
        self._populate()

    def refresh_items(self, items: list[dict]) -> None:
        self._items = list(items)
        if self.is_mounted:
            self._populate()

    def _populate(self) -> None:
        options = self.query_one("#subagent-list", OptionList)
        empty = self.query_one("#subagent-empty", Static)
        previous_id = (
            self._ids[options.highlighted]
            if options.highlighted is not None and 0 <= options.highlighted < len(self._ids)
            else None
        )
        options.clear_options()
        self._ids = []
        if not self._items:
            options.display = False
            empty.display = True
            return
        options.display = True
        empty.display = False
        for item in self._items:
            agent_id = item["id"]
            desc = item.get("description") or tr(self, "ui.subagent.no_description")
            runs = item.get("runs", 0)
            title = f"{agent_id} · {desc}" if runs <= 1 else f"{agent_id} ({runs}×) · {desc}"
            status = tr(self, f"ui.subagent.state.{item.get('status', 'done')}")
            updated = relative_time(item.get("updated_at", 0.0), owner=self)
            self._ids.append(agent_id)
            options.add_option(Option(SubagentRow(title, status, updated), id=agent_id))
        if previous_id in self._ids:
            options.highlighted = self._ids.index(previous_id)
        else:
            options.highlighted = 0
        options.focus()

    def action_move(self, direction: int) -> None:
        options = self.query_one("#subagent-list", OptionList)
        if not self._ids:
            return
        if direction < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()

    def action_choose(self) -> None:
        options = self.query_one("#subagent-list", OptionList)
        if options.highlighted is not None and 0 <= options.highlighted < len(self._ids):
            self.dismiss(self._ids[options.highlighted])

    @on(OptionList.OptionSelected, "#subagent-list")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._ids):
            self.dismiss(self._ids[event.option_index])


class SubagentTraceScreen(SelectionScreenMixin, ModalScreen[None]):
    """Read-only modal displaying a sub-agent's persisted and live execution trace."""

    CSS = """
    SubagentTraceScreen {
        align: center middle;
        background: $background;
    }
    SubagentTraceScreen > #trace-box {
        width: 100%; height: 100%;
        background: $background; color: $foreground;
    }
    SubagentTraceScreen #trace-header {
        height: 1; padding: 0 1;
        background: $background; color: #7C3AED; text-style: bold;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    SubagentTraceScreen #trace-transcript {
        height: 1fr;
    }
    SubagentTraceScreen #trace-hints {
        height: 1; padding: 0 1; text-style: dim;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", priority=True, show=False),
        Binding("ctrl+o", "tools", priority=True, show=False),
        Binding("ctrl+t", "thinking", priority=True, show=False),
        Binding("pageup", "page(-1)", priority=True, show=False),
        Binding("pagedown", "page(1)", priority=True, show=False),
        Binding("ctrl+home", "top", priority=True, show=False),
    ]

    def __init__(
        self,
        agent_id: str,
        entries: list[dict],
        *,
        description: str = "",
        status: str = "done",
    ) -> None:
        super().__init__()
        self.agent_id = agent_id
        self._initial_entries = entries
        self.description = description
        self.status = status
        self._loaded = False

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-box"):
            yield Static("", id="trace-header", markup=False)
            yield Transcript(id="trace-transcript", target_agent=self.agent_id)
            yield Static(tr(self, "ui.subagent.trace_hints"), id="trace-hints", markup=False)

    @property
    def trace_transcript(self) -> Transcript:
        return self.query_one("#trace-transcript", Transcript)

    async def on_mount(self) -> None:
        self.trace_transcript.translator = getattr(self.app.session, "translator", None)
        self._render_header()
        await self.trace_transcript.load_history(self._initial_entries)
        self._loaded = True
        self.trace_transcript.focus()

    def set_status(self, status: str, *, description: str | None = None) -> None:
        self.status = status
        if description:
            self.description = description
        if self.is_mounted:
            self._render_header()

    def _render_header(self) -> None:
        status_label = tr(self, f"ui.subagent.state.{self.status}")
        title = Text(no_wrap=True, overflow="ellipsis")
        title.append(self.agent_id, style="bold #7C3AED")
        title.append(f" · {status_label}")
        if self.description:
            title.append(f" · {self.description}", style="dim")
        update_static(self.query_one("#trace-header", Static), title, layout=False)

    async def consume_event(self, ev: AgentEvent) -> None:
        if not self._loaded or ev.agent != self.agent_id:
            return
        await self.trace_transcript.consume(ev)
        if ev.type == "agent_end":
            self.trace_transcript.finish_activity()

    def action_tools(self) -> None:
        self.trace_transcript.toggle_tools()

    def action_thinking(self) -> None:
        self.trace_transcript.toggle_thinking()

    def action_page(self, direction: int) -> None:
        if direction < 0:
            self.trace_transcript.action_page_up()
        else:
            self.trace_transcript.action_page_down()

    def action_top(self) -> None:
        self.trace_transcript.action_scroll_home()
