"""Select one fixed permission mode for the next task."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ..i18n import tr
from .choices import ChoiceList


def permission_flags(owner, mode, *, compact: bool = False) -> str:
    if mode.id == "no-limit":
        return tr(owner, "sandbox.host_execution")
    network = tr(owner, "ui.modes.on" if mode.network else "ui.modes.off")
    return tr(owner, "ui.modes.flags_short" if compact else "ui.modes.flags",
              read_paths=len(mode.read_paths), write_paths=len(mode.write_paths), network_allowed=network)


class PermissionModePicker(ModalScreen[str | None]):
    DEFAULT_CSS = """
    PermissionModePicker { align: center middle; }
    PermissionModePicker > Vertical {
        width: 92%; max-width: 90; height: auto; padding: 0 1;
        border: solid #7C3AED; background: $background; color: $foreground;
    }
    PermissionModePicker #modes-title { height: 1; text-style: bold; color: #7C3AED; }
    PermissionModePicker #modes-list { height: 5; border: none; padding: 0; }
    PermissionModePicker #modes-actions { height: auto; }
    PermissionModePicker Button { width: 1fr; min-width: 0; padding: 0; }
    """
    BINDINGS = [Binding("escape", "cancel", priority=True, show=False)]

    def __init__(self, modes: dict, selected_mode_id: str, *, translator):
        super().__init__()
        self.translator = translator
        self.modes = modes
        self.selected_mode_id = selected_mode_id

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(tr(self, "ui.modes.title"), id="modes-title", markup=False)
            yield ChoiceList(id="modes-list")
            with Horizontal(id="modes-actions"):
                yield Button(tr(self, "ui.modes.use"), id="mode-use")
                yield Button(tr(self, "ui.modes.grants"), id="mode-grants")
                yield Button(tr(self, "ui.modes.close"), id="mode-close")

    def on_mount(self) -> None:
        choices = self.query_one("#modes-list", ChoiceList)
        choices.add_options(
            Option(Text(f"{mode.name} · {permission_flags(self, mode, compact=True)}"), id=mode.id)
            for mode in self.modes.values()
        )
        choices.highlighted = list(self.modes).index(self.selected_mode_id)
        choices.focus()

    @on(OptionList.OptionSelected, "#modes-list")
    def mode_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    @on(Button.Pressed)
    def selected(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "mode-grants":
            self.dismiss("grants")
        elif event.button.id == "mode-use":
            choices = self.query_one("#modes-list", ChoiceList)
            if choices.highlighted is not None:
                self.dismiss(choices.get_option_at_index(choices.highlighted).id)
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)
