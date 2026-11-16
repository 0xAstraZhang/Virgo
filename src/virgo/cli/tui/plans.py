"""A version-bound plan viewer/editor; mutations use the existing commands."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from ..i18n import tr


class PlanEditor(ModalScreen[str | None]):
    DEFAULT_CSS = """
    PlanEditor { align: center middle; }
    PlanEditor > Vertical {
        width: 90%; height: 90%; padding: 1 2; border: solid #7C3AED;
        background: $background; color: $foreground; overflow-y: auto;
    }
    PlanEditor #plan-title { height: 1; text-style: bold; color: #7C3AED; }
    PlanEditor #plan-version { height: auto; text-style: dim; margin-bottom: 1; }
    PlanEditor #plan-markdown {
        height: 1fr; min-height: 3; border: solid #7C3AED;
        background: $background; color: $foreground; scrollbar-size-vertical: 1;
    }
    PlanEditor #plan-hints { height: auto; margin-top: 1; text-style: dim; }
    PlanEditor #plan-actions { height: auto; margin-top: 1; }
    PlanEditor Button { width: 1fr; min-width: 0; margin-right: 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", priority=True, show=False),
        Binding("ctrl+s", "save", priority=True, show=False),
    ]

    def __init__(self, plan, *, translator):
        super().__init__()
        self.translator = translator
        self.markdown = plan.markdown
        self.token = plan.token

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(tr(self, "ui.plan.title"), id="plan-title", markup=False)
            yield Static(tr(self, "ui.plan.token", token=self.token), id="plan-version", markup=False)
            yield TextArea(self.markdown, id="plan-markdown")
            yield Static(tr(self, "ui.plan.hints"), id="plan-hints", markup=False)
            with Horizontal(id="plan-actions"):
                yield Button(tr(self, "ui.plan.save"), id="plan-save", disabled=True)
                yield Button(tr(self, "ui.plan.execute"), id="plan-execute")
                yield Button(tr(self, "ui.plan.cancel"), id="plan-cancel")

    def on_mount(self) -> None:
        self.query_one("#plan-markdown", TextArea).focus()

    @on(TextArea.Changed, "#plan-markdown")
    def edited(self) -> None:
        text = self.query_one("#plan-markdown", TextArea).text
        changed = text != self.markdown
        self.query_one("#plan-execute", Button).disabled = changed
        self.query_one("#plan-save", Button).disabled = not changed or not text.strip()

    @on(Button.Pressed)
    def selected(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "plan-save":
            self.action_save()
        elif event.button.id == "plan-execute":
            if self.query_one("#plan-markdown", TextArea).text == self.markdown:
                self.dismiss("/plan execute " + self.token)
        else:
            self.action_cancel()

    def action_save(self) -> None:
        text = self.query_one("#plan-markdown", TextArea).text
        if text.strip() and text != self.markdown:
            self.dismiss(f"/plan edit {self.token}\n{text}")

    def action_cancel(self) -> None:
        self.dismiss(None)
