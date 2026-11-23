"""Inline permission choices and the saved-session picker."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import OptionList, Static

from ..i18n import tr
from .bridge import Confirmation
from .choices import MarkedChoiceList
from .sessions import SessionPicker as SessionPicker


class ApprovalPicker(Vertical):
    DEFAULT_CSS = """
    ApprovalPicker {
        height: auto; border-left: solid #7C3AED; padding: 0 1;
        background: $background; color: $foreground;
    }
    ApprovalPicker > #approval-title { height: 1; text-style: dim; }
    ApprovalPicker > #approval-question { height: auto; }
    ApprovalPicker #approval-question-text { height: auto; }
    ApprovalPicker > #approval-options {
        height: auto; border: none; padding: 0; background: $background;
        background-tint: transparent;
    }
    ApprovalPicker > #approval-help { height: auto; text-style: dim; }
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
        self.answers = self.ANSWERS
        self._answered = False
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="approval-title", markup=False)
        with VerticalScroll(id="approval-question"):
            yield Static("", id="approval-question-text", markup=False)
        yield MarkedChoiceList(id="approval-options")
        yield Static("", id="approval-help", markup=False)

    def open(self, confirmation: Confirmation) -> None:
        self.confirmation = confirmation
        self._answered = False
        self.display = True
        self.answers = ("y", "n") if confirmation.once_only else self.ANSWERS
        title = tr(self, "ui.approval.title", agent=confirmation.agent)
        if confirmation.once_only:
            title += " · " + tr(self, "ui.approval.once_scope")
        self.query_one("#approval-title", Static).update(
            title
        )
        question = confirmation.question.rstrip().removesuffix(tr(self, "ui.approval.prompt_hint")).rstrip()
        self.query_one("#approval-question-text", Static).update(Text(question))
        self.query_one("#approval-question").scroll_home(animate=False)
        options = self.query_one(MarkedChoiceList)
        choices = [
            ("allow", tr(self, "ui.approval.once" if confirmation.once_only else "ui.approval.allow_session")),
            ("deny", tr(self, "ui.approval.deny")),
        ]
        if not confirmation.once_only:
            choices.append(("always", tr(self, "ui.approval.allow_project")))
        options.set_choices(choices, highlighted=1)
        self.resize_panel()
        self.focus_control()

    def close(self) -> None:
        self.display = False
        self.confirmation = None
        self._answered = True

    def focus_control(self) -> None:
        self.query_one("#approval-options").focus()

    def resize_panel(self) -> None:
        # Reserve space for the conversation, options and help even for a long command.
        self.query_one("#approval-question").styles.max_height = max(
            1, min(8, self.app.size.height // 2 - 5)
        )
        help_text = (
            tr(self, "ui.approval.hints_short") if self.app.size.width < 64 else tr(self, "ui.approval.hints")
        )
        self.query_one("#approval-help", Static).update(Text(help_text))

    def on_resize(self) -> None:
        if self.confirmation is not None:
            self.resize_panel()

    def action_move(self, direction: int) -> None:
        if self.confirmation is not None and not self._answered:
            self.query_one(MarkedChoiceList).move_selection(direction)
            self.focus_control()

    def action_question_page(self, direction: int) -> None:
        question = self.query_one("#approval-question", VerticalScroll)
        if direction < 0:
            question.action_page_up()
        else:
            question.action_page_down()

    def action_choose(self) -> None:
        index = self.query_one(MarkedChoiceList).highlighted
        if index is not None:
            self.action_answer(self.answers[index])

    @on(OptionList.OptionSelected, "#approval-options")
    def answer_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        options = self.query_one(MarkedChoiceList)
        if event.option_index < options.option_count and event.option is options.get_option_at_index(
            event.option_index
        ):
            self.action_answer(self.answers[event.option_index])

    def action_answer(self, answer: str) -> None:
        if self.confirmation is not None and not self._answered and answer in self.answers:
            self._answered = True
            self.post_message(self.Answered(self.confirmation, answer))
