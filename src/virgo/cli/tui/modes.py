"""Permission preset selection and editing without changing task intent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, Switch
from textual.widgets.option_list import Option

from ...harness.execution_modes import NETWORK_DIRECTIONS, network_config
from ..i18n import tr
from .choices import ChoiceList


def permission_flags(owner, mode, *, compact: bool = False) -> str:
    if mode.id == "host":
        return tr(owner, "sandbox.host_execution")
    selection = network_config(mode.network)
    if isinstance(selection, bool):
        network = tr(owner, "ui.modes.on" if selection else "ui.modes.off")
    else:
        network = ", ".join(tr(owner, "ui.modes." + direction) for direction in selection)
    return tr(owner, "ui.modes.flags_short" if compact else "ui.modes.flags",
              read_paths=len(mode.read_paths), write_paths=len(mode.write_paths), network_allowed=network)



@dataclass(frozen=True)
class PermissionModeChoice:
    action: str
    mode_id: str
    name: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    network: frozenset[str]


class PermissionModePicker(ModalScreen[PermissionModeChoice | None]):
    """Save edits separately from selecting a preset for the current conversation."""

    DEFAULT_CSS = """
    PermissionModePicker { align: center middle; }
    PermissionModePicker > Vertical {
        width: 92%; max-width: 90; height: 92%; padding: 0 1;
        border: solid #7C3AED; background: $background; color: $foreground;
    }
    PermissionModePicker #modes-title { height: 1; text-style: bold; color: #7C3AED; }
    PermissionModePicker #modes-content { height: 1fr; scrollbar-size-vertical: 1; }
    PermissionModePicker #modes-list { height: 5; border: none; padding: 0; }
    PermissionModePicker #mode-name-label { height: 1; }
    PermissionModePicker #mode-name { height: 3; }
    PermissionModePicker .mode-flag { height: 3; align-vertical: middle; }
    PermissionModePicker .mode-flag Static { width: 1fr; height: 1; }
    PermissionModePicker .mode-flag Switch { width: auto; }
    PermissionModePicker #modes-hints { height: auto; text-style: dim; }
    PermissionModePicker #modes-actions { height: auto; }
    PermissionModePicker Button { width: 1fr; min-width: 0; padding: 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", priority=True, show=False),
        Binding("ctrl+s", "save", priority=True, show=False),
    ]

    def __init__(self, modes: dict, selected_mode_id: str, *, translator):
        super().__init__()
        self.translator = translator
        self.modes = dict(modes)
        self.mode_id = selected_mode_id
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(tr(self, "ui.modes.title"), id="modes-title", markup=False)
            with VerticalScroll(id="modes-content"):
                yield ChoiceList(id="modes-list")
                yield Static(tr(self, "ui.modes.name"), id="mode-name-label", markup=False)
                yield Input(id="mode-name", max_length=100)
                for field in ("read_paths", "write_paths"):
                    yield Static(tr(self, "ui.modes." + field) + " (JSON)", markup=False)
                    yield Input(id="mode-" + field)
                for flag in NETWORK_DIRECTIONS:
                    with Horizontal(classes="mode-flag"):
                        yield Static(tr(self, "ui.modes." + flag), markup=False)
                        yield Switch(id="mode-" + flag)
                yield Static(tr(self, "ui.modes.hints"), id="modes-hints", markup=False)
            with Horizontal(id="modes-actions"):
                yield Button(tr(self, "ui.modes.use"), id="mode-use")
                yield Button(tr(self, "ui.modes.save"), id="mode-save", disabled=True)
                yield Button(tr(self, "ui.modes.new"), id="mode-new")
                yield Button(tr(self, "ui.modes.close"), id="mode-close")

    def on_mount(self) -> None:
        choices = self.query_one("#modes-list", ChoiceList)
        choices.add_options(
            Option(Text(f"{mode.name} · {permission_flags(self, mode, compact=True)}"), id=mode_id)
            for mode_id, mode in self.modes.items()
        )
        selected = list(self.modes).index(self.mode_id) if self.mode_id in self.modes else 0
        choices.highlighted = selected
        self.load_mode(list(self.modes)[selected])
        choices.focus()

    def load_mode(self, mode_id: str) -> None:
        mode = self.modes[mode_id]
        self.mode_id = mode_id
        self._loading = True
        self.query_one("#mode-name", Input).value = mode.name
        for field in ("read_paths", "write_paths"):
            self.query_one("#mode-" + field, Input).value = json.dumps(getattr(mode, field), ensure_ascii=False)
        for flag in NETWORK_DIRECTIONS:
            self.query_one("#mode-" + flag, Switch).value = flag in mode.network
        self._loading = False
        self.refresh_actions()

    @on(OptionList.OptionHighlighted, "#modes-list")
    def mode_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        choices = self.query_one("#modes-list", ChoiceList)
        if (event.option_index == choices.highlighted
                and event.option is choices.get_option_at_index(event.option_index)
                and event.option.id in self.modes):
            self.load_mode(event.option.id)

    @on(OptionList.OptionSelected, "#modes-list")
    def mode_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        choices = self.query_one("#modes-list", ChoiceList)
        if event.option is choices.get_option_at_index(event.option_index) and event.option.id in self.modes:
            self.load_mode(event.option.id)
            self.use_mode()

    def choice(self, action: str) -> PermissionModeChoice:
        def paths(field):
            value = json.loads(self.query_one("#mode-" + field, Input).value)
            if not isinstance(value, list) or any(not isinstance(p, str) or not p or "\0" in p for p in value):
                raise ValueError("Paths must be a JSON array of nonempty strings")
            return tuple(value)

        return PermissionModeChoice(
            action,
            self.mode_id,
            self.query_one("#mode-name", Input).value.strip(),
            read_paths=paths("read_paths"),
            write_paths=paths("write_paths"),
            network=frozenset(flag for flag in NETWORK_DIRECTIONS if self.query_one("#mode-" + flag, Switch).value),
        )

    def refresh_actions(self) -> None:
        if self._loading or not self.is_mounted:
            return
        try:
            choice = self.choice("save")
            if any(not isinstance(p, str) or not p for p in (*choice.read_paths, *choice.write_paths)):
                raise ValueError("Invalid paths")
        except (ValueError, TypeError):
            self.query_one("#mode-save", Button).disabled = True
            self.query_one("#mode-use", Button).disabled = True
            return
        original = self.modes.get(self.mode_id)
        changed = original is None or any(
            getattr(original, field) != getattr(choice, field)
            for field in ("name", "read_paths", "write_paths", "network")
        )
        self.query_one("#mode-save", Button).disabled = not choice.name or not changed
        self.query_one("#mode-use", Button).disabled = changed

    @on(Input.Changed)
    @on(Switch.Changed)
    def edited(self) -> None:
        self.refresh_actions()

    @on(Button.Pressed)
    def selected(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "mode-use":
            self.use_mode()
        elif event.button.id == "mode-save":
            self.action_save()
        elif event.button.id == "mode-new":
            self.mode_id = "custom_" + uuid4().hex[:12]
            self.query_one("#modes-list", ChoiceList).highlighted = None
            self.query_one("#mode-name", Input).value = tr(self, "ui.modes.custom")
            self.query_one("#mode-name", Input).focus()
            self.refresh_actions()
        else:
            self.action_cancel()

    def use_mode(self) -> None:
        self.refresh_actions()
        if not self.query_one("#mode-use", Button).disabled:
            self.dismiss(self.choice("select"))

    def action_save(self) -> None:
        self.refresh_actions()
        if not self.query_one("#mode-save", Button).disabled:
            self.dismiss(self.choice("save"))

    def action_cancel(self) -> None:
        self.dismiss(None)
