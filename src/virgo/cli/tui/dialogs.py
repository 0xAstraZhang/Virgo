"""Keyboard accessible permission and saved-session pickers."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option


class ApprovalScreen(ModalScreen[str | None]):
    AUTO_FOCUS = "#deny"
    BINDINGS = [
        Binding("y", "answer('y')", show=False),
        Binding("n,escape", "answer('n')", show=False),
        Binding("a", "answer('a')", show=False),
    ]

    def __init__(self, confirmation):
        super().__init__()
        self.confirmation = confirmation

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(f"Permission · {self.confirmation.agent}", style="bold"))
            with VerticalScroll(classes="question"):
                yield Static(Text(self.confirmation.question))
            with Horizontal(classes="dialog-buttons"):
                yield Button(Text("Allow [y]"), id="allow")
                yield Button(Text("Deny [n]"), id="deny")
                yield Button(Text("Session [a]"), id="always")

    @on(Button.Pressed)
    def answer_button(self, event: Button.Pressed) -> None:
        self.action_answer({"allow": "y", "deny": "n", "always": "a"}[event.button.id])

    def action_answer(self, answer: str) -> None:
        self.dismiss(answer)


class SessionPicker(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, sessions: list[dict]):
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("Resume a session", style="bold"))
            if self.sessions:
                yield OptionList(
                    *(
                        Option(Text(f"{item['title']}  ·  {item['messages']} messages"), id=str(i))
                        for i, item in enumerate(self.sessions)
                    ),
                    id="sessions",
                )
            else:
                yield Static("No saved sessions for this directory.", markup=False)
            yield Button("Cancel · Esc", id="cancel")

    def on_mount(self) -> None:
        if self.sessions:
            self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(Path(self.sessions[event.option_index]["path"]))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
