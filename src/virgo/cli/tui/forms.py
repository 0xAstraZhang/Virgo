"""Multiline form values, independent from the chat editor and its history."""

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class MultilineInput(TextArea):
    BINDINGS = [Binding("ctrl+s", "submit", priority=True, show=False)]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))
