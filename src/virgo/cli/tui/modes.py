"""Select one fixed permission mode for the next task."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static

from ...execution_policy import ExecutionMode
from ...i18n import Translator
from ..i18n import tr
from .choices import MarkedChoiceList


def permission_flags(owner, mode) -> str:
    if mode.id == "no-limit":
        return tr(owner, "ui.modes.host_flags")
    network = tr(owner, "ui.modes.on" if mode.network else "ui.modes.off")
    return tr(owner, "ui.modes.flags_short",
              read_paths=len(mode.read_paths), write_paths=len(mode.write_paths), network_allowed=network)


class PermissionModePicker(Vertical):
    DEFAULT_CSS = """
    PermissionModePicker {
        height: auto; padding: 0 1; border-top: solid #7C3AED;
        background: $background; color: $foreground;
    }
    PermissionModePicker #modes-title { height: 1; text-style: bold; color: #7C3AED; }
    PermissionModePicker #modes-list {
        height: auto; border: none; padding: 0; text-wrap: nowrap; text-overflow: ellipsis;
    }
    PermissionModePicker #modes-help { height: 1; text-style: dim; text-wrap: nowrap; }
    """
    BINDINGS = [
        Binding("up,left", "move(-1)", priority=True, show=False),
        Binding("down,right,tab,shift+tab", "move(1)", priority=True, show=False),
        Binding("enter", "choose", priority=True, show=False),
        Binding("1", "choose(0)", priority=True, show=False),
        Binding("2", "choose(1)", priority=True, show=False),
        Binding("3", "choose(2)", priority=True, show=False),
        Binding("g", "answer('grants')", priority=True, show=False),
        Binding("escape", "answer(None)", priority=True, show=False),
    ]

    class Answered(Message):
        def __init__(self, choice: str | None) -> None:
            self.choice = choice
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False
        self.pending_answer: PermissionModePicker.Answered | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="modes-title", markup=False)
        yield MarkedChoiceList(id="modes-list")
        yield Static("", id="modes-help", markup=False)

    def open(self, modes: dict[str, ExecutionMode], selected_mode_id: str, *, translator: Translator) -> None:
        self.translator = translator
        self.display = True
        self.query_one("#modes-title", Static).update(tr(self, "ui.modes.title"))
        rows = []
        for number, mode in enumerate(modes.values(), 1):
            label = Text(f"{number}. {mode.name}")
            if mode.id == selected_mode_id:
                label.append(" ✓", style="green")
            label.append(" · " + permission_flags(self, mode))
            rows.append((mode.id, label))
        self.query_one(MarkedChoiceList).set_choices(rows, list(modes).index(selected_mode_id))
        self.update_help()
        self.focus_control()

    def update_help(self) -> None:
        key = "ui.modes.help_short" if self.app.size.width < 64 else "ui.modes.help"
        self.query_one("#modes-help", Static).update(tr(self, key))

    def on_resize(self) -> None:
        if self.display:
            self.update_help()

    def focus_control(self) -> None:
        self.query_one(MarkedChoiceList).focus()

    def close(self) -> None:
        self.display = False
        self.pending_answer = None

    def action_move(self, direction: int) -> None:
        choices = self.query_one(MarkedChoiceList)
        choices.move_selection(direction)
        choices.focus()

    def action_choose(self, index: int | None = None) -> None:
        choices = self.query_one(MarkedChoiceList)
        if index is None:
            index = choices.highlighted
        if index is not None and index < choices.option_count:
            self.action_answer(choices.get_option_at_index(index).id)

    @on(OptionList.OptionSelected, "#modes-list")
    def mode_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        choices = self.query_one(MarkedChoiceList)
        if event.option_index < choices.option_count and event.option is choices.get_option_at_index(
            event.option_index
        ):
            self.action_answer(event.option.id)

    def action_answer(self, choice: str | None) -> None:
        if self.display and self.pending_answer is None:
            self.pending_answer = self.Answered(choice)
            self.post_message(self.pending_answer)
