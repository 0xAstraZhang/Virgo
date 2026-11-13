"""Immediate header folds with independently selectable detail bodies."""

from __future__ import annotations

from textual import on
from textual.containers import Vertical
from textual.events import Click, MouseDown
from textual.geometry import Offset
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static


class FoldHeader(Static, can_focus=True):
    """A non-selectable mouse and keyboard button for its enclosing fold."""

    ALLOW_SELECT = False
    BINDINGS = [("enter,space", "toggle", "Toggle details")]
    DEFAULT_CSS = """
    FoldHeader {
        height: 1; min-height: 1; background: $background;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    FoldHeader:focus { text-style: reverse; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mouse_down: Offset | None = None

    def on_mouse_down(self, event: MouseDown) -> None:
        self._mouse_down = event.screen_offset if event.button == 1 else None

    class Toggled(Message):
        def __init__(self, header: FoldHeader) -> None:
            self.header = header
            super().__init__()

    def action_toggle(self) -> None:
        self.post_message(self.Toggled(self))

    def on_click(self, event: Click) -> None:
        if event.button == 1:
            event.stop()
            start, self._mouse_down = self._mouse_down, None
            if event.chain == 1 and (start is None or start == event.screen_offset):
                self.action_toggle()


class Foldable(Vertical):
    """Only a nested fold's own header controls it; bodies keep selection gestures."""

    def action_toggle(self) -> None:
        raise NotImplementedError

    def _owns_fold_gesture(self, widget: Widget | None) -> bool:
        if widget is None:
            return False
        return next((node for node in widget.ancestors_with_self if isinstance(node, Foldable)), None) is self

    @on(FoldHeader.Toggled)
    def toggle_header(self, event: FoldHeader.Toggled) -> None:
        if self._owns_fold_gesture(event.header):
            event.stop()
            self.action_toggle()
