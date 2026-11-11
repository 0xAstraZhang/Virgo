"""Accessible whole-block folds which leave text selection gestures intact."""

from __future__ import annotations

from textual import on
from textual.containers import Vertical
from textual.events import Click, MouseDown
from textual.geometry import Offset
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static


class FoldHeader(Static, can_focus=True):
    """Keyboard fold handle; its enclosing Foldable owns mouse gestures."""

    BINDINGS = [("enter,space", "toggle", "Toggle details")]
    DEFAULT_CSS = """
    FoldHeader {
        height: 1; min-height: 1; background: $background;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    FoldHeader:focus { text-style: reverse; }
    """

    class Toggled(Message):
        def __init__(self, header: FoldHeader) -> None:
            self.header = header
            super().__init__()

    def action_toggle(self) -> None:
        self.post_message(self.Toggled(self))


class Foldable(Vertical):
    """Call ``action_toggle`` on a single click anywhere in this block.

    Nested Foldables own their own gestures. Double/triple clicks still bubble
    to the application so its selection-copy handler sees the completed text
    selection. Waiting for the click chain also keeps the selected body mounted.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fold_timer: Timer | None = None
        self._fold_mouse_down: Offset | None = None

    def action_toggle(self) -> None:
        """Override to update this block's expanded state."""
        raise NotImplementedError

    def _owns_fold_gesture(self, widget: Widget | None) -> bool:
        if widget is None:
            return False
        return next((node for node in widget.ancestors_with_self if isinstance(node, Foldable)), None) is self

    def _cancel_fold_click(self) -> None:
        if self._fold_timer is not None:
            self._fold_timer.stop()
            self._fold_timer = None

    def on_mouse_down(self, event: MouseDown) -> None:
        if self._owns_fold_gesture(event.widget):
            self._cancel_fold_click()
            self._fold_mouse_down = event.screen_offset if event.button == 1 else None

    def on_click(self, event: Click) -> None:
        if not self._owns_fold_gesture(event.widget):
            return
        self._cancel_fold_click()
        start, self._fold_mouse_down = self._fold_mouse_down, None
        if (
            event.button != 1
            or event.chain != 1
            or (start is not None and start != event.screen_offset)
            or self.screen.get_selected_text()
        ):
            return
        # Do not stop bubbling: SelectionCopyMixin observes multi-clicks at App.
        self._fold_timer = self.set_timer(self.app.CLICK_CHAIN_TIME_THRESHOLD, self._finish_fold_click)

    def _finish_fold_click(self) -> None:
        self._fold_timer = None
        if self.is_mounted and not self.screen.get_selected_text():
            self.action_toggle()

    @on(FoldHeader.Toggled)
    def toggle_header(self, event: FoldHeader.Toggled) -> None:
        if self._owns_fold_gesture(event.header):
            event.stop()
            self._cancel_fold_click()
            self.action_toggle()

    def on_unmount(self) -> None:
        self._cancel_fold_click()
