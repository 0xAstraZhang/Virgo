"""Inline permission choices and the saved-session picker."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .bridge import Confirmation
from .choices import ChoiceList
from .sessions import SessionPicker as SessionPicker


class ApprovalPicker(Vertical):
    DEFAULT_CSS = """
    ApprovalPicker {
        height: auto; border-left: solid #7C3AED; padding: 1 2;
        background: $background; color: $foreground;
    }
    ApprovalPicker > #approval-title { height: 1; text-style: dim; margin-bottom: 1; }
    ApprovalPicker > #approval-question { height: auto; margin-bottom: 1; }
    ApprovalPicker #approval-question-text { height: auto; }
    ApprovalPicker > #approval-options {
        height: auto; border: none; padding: 0; background: $background;
        background-tint: transparent;
    }
    ApprovalPicker > #approval-help { height: auto; text-style: dim; margin-top: 1; }
    """
    ANSWERS = ("y", "n", "a")
    BINDINGS = [
        Binding("up,left,shift+tab", "move(-1)", priority=True, show=False),
        Binding("down,right,tab", "move(1)", priority=True, show=False),
        Binding("enter", "choose", priority=True, show=False),
        Binding("y,1", "answer('y')", priority=True, show=False),
        Binding("n,2,escape", "answer('n')", priority=True, show=False),
        Binding("a,3", "answer('a')", priority=True, show=False),
        Binding("pageup", "question_page(-1)", priority=True, show=False),
        Binding("pagedown", "question_page(1)", priority=True, show=False),
    ]

    class Answered(Message):
        def __init__(self, confirmation: Confirmation, answer: str) -> None:
            self.confirmation = confirmation
            self.answer = answer
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.confirmation: Confirmation | None = None
        self._answered = False
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="approval-title", markup=False)
        with VerticalScroll(id="approval-question"):
            yield Static("", id="approval-question-text", markup=False)
        yield ChoiceList(id="approval-options")
        yield Static("", id="approval-help", markup=False)

    def open(self, confirmation: Confirmation) -> None:
        self.confirmation = confirmation
        self._answered = False
        self.display = True
        self.query_one("#approval-title", Static).update(f"Permission · {confirmation.agent}")
        question = confirmation.question.removesuffix("  Allow? [y]es / [n]o / [a]lways this session")
        self.query_one("#approval-question-text", Static).update(Text(question))
        self.query_one("#approval-question").scroll_home(animate=False)
        options = self.query_one("#approval-options", ChoiceList)
        options.clear_options()
        options.add_options([
            Option("1. Allow once [y]", id="allow"),
            Option("2. Deny [n]", id="deny"),
            Option("3. Allow this session [a]", id="always"),
        ])
        options.highlighted = 1
        self.resize_panel()
        self.focus_control()

    def close(self) -> None:
        self.display = False
        self.confirmation = None
        self._answered = True

    def focus_control(self) -> None:
        self.query_one("#approval-options").focus()

    def resize_panel(self) -> None:
        compact = self.app.size.height < 20
        self.styles.padding = (0, 1) if compact else (1, 2)
        self.query_one("#approval-title").styles.margin = (0, 0, 0 if compact else 1, 0)
        self.query_one("#approval-question").styles.margin = (0, 0, 0 if compact else 1, 0)
        self.query_one("#approval-help").styles.margin = (0 if compact else 1, 0, 0, 0)
        # Reserve space for the conversation, options and help even for a long command.
        self.query_one("#approval-question").styles.max_height = max(
            1, min(8, self.app.size.height // 2 - (5 if compact else 7))
        )
        help_text = (
            "↑↓ select · Enter · Esc deny"
            if self.app.size.width < 64
            else "↑↓ select · Enter confirm · Esc deny · PgUp/PgDn scroll"
        )
        self.query_one("#approval-help", Static).update(Text(help_text))

    def on_resize(self) -> None:
        if self.confirmation is not None:
            self.resize_panel()

    def action_move(self, direction: int) -> None:
        if self.confirmation is not None and not self._answered:
            self.query_one("#approval-options", ChoiceList).move_selection(direction)
            self.focus_control()

    def action_question_page(self, direction: int) -> None:
        question = self.query_one("#approval-question", VerticalScroll)
        if direction < 0:
            question.action_page_up()
        else:
            question.action_page_down()

    def action_choose(self) -> None:
        index = self.query_one("#approval-options", ChoiceList).highlighted
        if index is not None:
            self.action_answer(self.ANSWERS[index])

    @on(OptionList.OptionSelected, "#approval-options")
    def answer_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        options = self.query_one("#approval-options", ChoiceList)
        if event.option is options.get_option_at_index(event.option_index):
            self.action_answer(self.ANSWERS[event.option_index])

    def action_answer(self, answer: str) -> None:
        if self.confirmation is not None and not self._answered:
            self._answered = True
            self.post_message(self.Answered(self.confirmation, answer))
