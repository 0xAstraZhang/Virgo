"""Keyboard accessible permission and saved-session pickers."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .sessions import SessionPicker as SessionPicker


class ApprovalScreen(ModalScreen[str | None]):
    AUTO_FOCUS = "#deny"
    BINDINGS = [
        Binding("left", "previous_answer", priority=True, show=False),
        Binding("right", "next_answer", priority=True, show=False),
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

    def action_previous_answer(self) -> None:
        self.focus_previous(Button)

    def action_next_answer(self) -> None:
        self.focus_next(Button)

    @on(Button.Pressed)
    def answer_button(self, event: Button.Pressed) -> None:
        self.action_answer({"allow": "y", "deny": "n", "always": "a"}[event.button.id])

    def action_answer(self, answer: str) -> None:
        self.dismiss(answer)
