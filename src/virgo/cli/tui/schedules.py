"""A keyboard-first workspace for schedule lists, details and forms."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..i18n import tr
from .bridge import InputRequest
from .forms import MultilineInput
from .transcript import update_static


@dataclass(frozen=True)
class ScheduleRow:
    title: str
    frequency: str
    next_run: str
    selected: bool = False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(3, options.max_width - 4)
        frequency = min(21, max(4, width // 4))
        next_run = min(16, max(4, width // 4))
        title = max(1, width - frequency - next_run)
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=title, no_wrap=True, overflow="ellipsis")
        table.add_column(width=frequency, overflow="fold")
        table.add_column(width=next_run, overflow="fold")
        prefix = "› " if self.selected else "  "
        table.add_row(
            Text(prefix + " ".join(self.title.split()), no_wrap=True, overflow="ellipsis"),
            Text(self.frequency),
            Text(self.next_run),
        )
        yield table


class ScheduleWorkspace(Vertical):
    can_focus = True
    DEFAULT_CSS = """
    ScheduleWorkspace { height: 1fr; width: 100%; padding: 1 2 0 2; }
    ScheduleWorkspace > Vertical { width: 100%; max-width: 100; height: 1fr; }
    ScheduleWorkspace #schedule-heading { height: auto; margin-bottom: 1; text-style: bold; }
    ScheduleWorkspace #schedule-notice { height: auto; max-height: 3; margin-bottom: 1; }
    ScheduleWorkspace #schedule-content { height: 1fr; }
    ScheduleWorkspace #schedule-columns { height: auto; margin-bottom: 1; }
    ScheduleWorkspace #schedule-list { height: 1fr; border: none; padding: 0;
        background: $background; scrollbar-size: 0 0; }
    ScheduleWorkspace #schedule-list:focus,
    ScheduleWorkspace #schedule-options:focus { border: none; background-tint: transparent; }
    ScheduleWorkspace OptionList > .option-list--option { padding: 0; }
    ScheduleWorkspace OptionList > .option-list--option-highlighted,
    ScheduleWorkspace OptionList:focus > .option-list--option-highlighted {
        background: $background; color: $foreground; text-style: reverse;
    }
    ScheduleWorkspace #schedule-options { height: auto; max-height: 50%; border: none;
        padding: 0; background: $background; scrollbar-size: 0 0; }
    ScheduleWorkspace #schedule-empty { height: auto; }
    ScheduleWorkspace #schedule-detail-scroll { height: 1fr; margin-bottom: 1; scrollbar-size: 1 1; }
    ScheduleWorkspace #schedule-details { height: auto; }
    ScheduleWorkspace #schedule-value { height: 3; border: none; border-bottom: solid $primary;
        padding: 0; background: $background; color: $foreground; }
    ScheduleWorkspace #schedule-multiline { height: 1fr; border: none; border-left: solid $primary;
        padding: 0 1; background: $background; color: $foreground; }
    ScheduleWorkspace #schedule-help { height: auto; margin-top: 1; }
    """
    BINDINGS = [Binding("n", "new", show=False)]

    class Answered(Message):
        def __init__(self, workspace: ScheduleWorkspace, request_id: int, answer: str | None) -> None:
            self.workspace = workspace
            self.request_id = request_id
            self.answer = answer
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False
        self.request: InputRequest | None = None
        self._selected_id: str | None = None
        self._selected_index = 0
        self._scroll_y = 0.0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(tr(self, "ui.schedule.title"), id="schedule-heading", markup=False)
            yield Static("", id="schedule-notice", markup=False)
            with Vertical(id="schedule-content"):
                yield Static(
                    ScheduleRow(
                        tr(self, "ui.schedule.column_title"),
                        tr(self, "ui.schedule.column_frequency"),
                        tr(self, "ui.schedule.column_next"),
                    ),
                    id="schedule-columns",
                )
                yield OptionList(id="schedule-list")
                yield Static(tr(self, "ui.schedule.empty"), id="schedule-empty", markup=False)
                with VerticalScroll(id="schedule-detail-scroll"):
                    yield Static("", id="schedule-details", markup=False)
                yield OptionList(id="schedule-options")
                yield Input(id="schedule-value")
                yield MultilineInput(id="schedule-multiline")
            yield Static("", id="schedule-help", markup=False)

    def begin(self) -> None:
        self._selected_id = None
        self._selected_index = 0
        self._scroll_y = 0
        self.display = True
        self.query_one("#schedule-notice").display = False
        self.refresh_language()
        self.resize_workspace()

    def refresh_language(self) -> None:
        self.query_one("#schedule-columns", Static).update(
            ScheduleRow(
                tr(self, "ui.schedule.column_title"),
                tr(self, "ui.schedule.column_frequency"),
                tr(self, "ui.schedule.column_next"),
            )
        )
        self.query_one("#schedule-empty", Static).update(tr(self, "ui.schedule.empty"))
        if self.request is None:
            self.query_one("#schedule-heading", Static).update(tr(self, "ui.schedule.title"))

    def end(self) -> None:
        self.request = None
        self.display = False
        self.query_one("#schedule-multiline", MultilineInput).clear()

    def open(self, request: InputRequest) -> None:
        self.request = request
        for selector in (
            "#schedule-columns",
            "#schedule-list",
            "#schedule-empty",
            "#schedule-detail-scroll",
            "#schedule-options",
            "#schedule-value",
            "#schedule-multiline",
        ):
            self.query_one(selector).display = False
        if request.schedules is not None:
            self._open_list(request)
            help_text = tr(self, "ui.schedule.list_hints")
        else:
            if request.details is not None:
                self.query_one("#schedule-details", Static).update(Text(request.details))
                self.query_one("#schedule-detail-scroll").display = True
                self.query_one("#schedule-detail-scroll").scroll_home(animate=False)
            if request.options is not None:
                choices = self.query_one("#schedule-options", OptionList)
                choices.clear_options()
                choices.add_options(Option(Text(label), id=str(i)) for i, label in enumerate(request.options))
                choices.highlighted = 0
                choices.display = True
                help_text = tr(self, "ui.schedule.select_hints")
                if request.details is not None:
                    help_text += tr(self, "ui.schedule.details_hints")
            elif request.multiline:
                field = self.query_one("#schedule-multiline", MultilineInput)
                field.load_text(request.default)
                field.display = True
                help_text = tr(self, "ui.schedule.multiline_hints")
            else:
                field = self.query_one("#schedule-value", Input)
                field.value = request.default
                field.display = True
                help_text = tr(self, "ui.schedule.input_hints")
        self.update_heading()
        self.query_one("#schedule-help", Static).update(Text(help_text))
        self.focus_control()

    def update_heading(self) -> None:
        if self.request is None:
            return
        widget = self.query_one("#schedule-heading", Static)
        heading = Text(self.request.question)
        if self.request.schedules is not None:
            # Use the scheduler's system-local zone, never text parsed from a title.
            zone = str(datetime.now().astimezone().tzinfo)
            heading.append(" " * max(2, widget.size.width - heading.cell_len - len(zone)))
            heading.append(zone, style="not bold")
        update_static(widget, heading)

    def _open_list(self, request: InputRequest) -> None:
        choices = self.query_one("#schedule-list", OptionList)
        choices.clear_options()
        rows = request.schedules
        self._selected_id = request.selected_schedule or self._selected_id
        ids = [row.id for row in rows]
        self._selected_index = (
            ids.index(self._selected_id)
            if self._selected_id in ids
            else min(self._selected_index, max(0, len(rows) - 1))
        )
        self._selected_id = ids[self._selected_index] if ids else None
        choices.add_options(
            Option(
                ScheduleRow(row.title, row.frequency, row.next_run, row.id == self._selected_id), id=row.id
            )
            for row in rows
        )
        choices.highlighted = self._selected_index if rows else None
        choices.display = bool(rows)
        self.query_one("#schedule-columns").display = True
        self.query_one("#schedule-empty").display = not rows
        self.call_after_refresh(self._restore_scroll)

    def _restore_scroll(self) -> None:
        if self.request is not None and self.request.schedules is not None:
            choices = self.query_one("#schedule-list", OptionList)
            choices.scroll_to(y=self._scroll_y, animate=False, immediate=True)
            choices.scroll_to_highlight()

    def close_request(self) -> None:
        self.request = None

    def focus_control(self) -> None:
        if self.request is None:
            return
        if self.request.schedules == ():
            self.focus()
            return
        selector = (
            "#schedule-list"
            if self.request.schedules is not None
            else "#schedule-options"
            if self.request.options is not None
            else "#schedule-multiline"
            if self.request.multiline
            else "#schedule-value"
        )
        self.query_one(selector).focus()

    def notice(self, text: str, style: str | None = None) -> None:
        notice = self.query_one("#schedule-notice", Static)
        notice.update(Text(text, style="bold" if style == "error" else ""))
        notice.display = True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "new":
            return self.request is not None and self.request.schedules is not None
        return True

    def action_new(self) -> None:
        self.answer("new")

    def answer(self, value: str | None) -> None:
        if self.request is None:
            return
        request = self.request
        if request.schedules is not None:
            self._scroll_y = self.query_one("#schedule-list", OptionList).scroll_y
        self.request = None
        self.query_one("#schedule-notice").display = False
        self.post_message(self.Answered(self, request.id, value))

    @on(OptionList.OptionSelected, "#schedule-list")
    def selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.request is not None and self.request.schedules is not None:
            self.answer(event.option_id)

    @on(OptionList.OptionHighlighted, "#schedule-list")
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if self.request is None or self.request.schedules is None:
            return
        ids = {row.id: row for row in self.request.schedules}
        if event.option_id not in ids or event.option_id == self._selected_id:
            return
        choices = self.query_one("#schedule-list", OptionList)
        previous = self._selected_id
        self._selected_id = event.option_id
        self._selected_index = event.option_index
        for key in (previous, self._selected_id):
            if key in ids:
                row = ids[key]
                choices.replace_option_prompt(
                    key, ScheduleRow(row.title, row.frequency, row.next_run, key == self._selected_id)
                )

    @on(OptionList.OptionSelected, "#schedule-options")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.request is not None and self.request.options is not None:
            self.answer(self.request.options[event.option_index])

    @on(Input.Submitted, "#schedule-value")
    def value_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.answer(event.value)

    @on(MultilineInput.Submitted)
    def multiline_submitted(self, event: MultilineInput.Submitted) -> None:
        event.stop()
        self.answer(event.value)

    def resize_workspace(self) -> None:
        compact = self.app.size.height < 18
        self.styles.padding = (0, 1) if compact else (1, 2, 0, 2)
        for selector in ("#schedule-heading", "#schedule-columns"):
            self.query_one(selector).styles.margin = (0, 0, 0 if compact else 1, 0)
        self.call_after_refresh(self.query_one("#schedule-list", OptionList).scroll_to_highlight)

    def on_resize(self) -> None:
        if self.is_mounted:
            self.resize_workspace()
            self.call_after_refresh(self.update_heading)
