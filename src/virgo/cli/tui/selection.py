"""Copy completed mouse selections to the local clipboard or terminal."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from rich.style import Style
from textual.dom import NoScreen
from textual.errors import NoWidget
from textual.events import Click, Event, MouseEvent, TextSelected
from textual.geometry import Offset, Region
from textual.screen import Screen
from textual.selection import SelectState
from textual.widget import Widget
from textual.widgets import Input, TextArea

from ..i18n import tr


def _copy_windows(text: str) -> None:
    import win32clipboard as clipboard

    try:
        for attempt in range(3):
            try:
                clipboard.OpenClipboard()
                break
            except (clipboard.error, OSError):
                if attempt == 2:
                    raise
                time.sleep(0.015)
        try:
            clipboard.EmptyClipboard()
            clipboard.SetClipboardData(clipboard.CF_UNICODETEXT, text)
        finally:
            clipboard.CloseClipboard()
    except clipboard.error as error:
        raise OSError("Windows clipboard unavailable") from error


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _clipboard_command() -> tuple[list[str], str] | None:
    if sys.platform == "darwin":
        return ["/usr/bin/pbcopy"], "utf-8"
    if sys.platform != "linux":
        return None
    if _is_wsl():
        clip = shutil.which("clip.exe")
        if not clip:
            clip = "/mnt/c/Windows/System32/clip.exe"
            if not Path(clip).is_file():
                raise FileNotFoundError(clip)
        return [clip], "utf-16-le"
    if os.environ.get("WAYLAND_DISPLAY") and (tool := shutil.which("wl-copy")):
        return [tool], "utf-8"
    if os.environ.get("DISPLAY"):
        if tool := shutil.which("xclip"):
            return [tool, "-selection", "clipboard"], "utf-8"
        if tool := shutil.which("xsel"):
            return [tool, "--clipboard", "--input"], "utf-8"
    return None


def _copy_locally(text: str) -> bool:
    if sys.platform == "win32":
        _copy_windows(text)
        return True
    local = _clipboard_command()
    if local is None:
        return False
    command, encoding = local
    subprocess.run(
        command, input=text.encode(encoding), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=1, check=True,
    )
    return True


class SelectionScreenMixin:
    """Guard Textual 8.2.8 hit-testing and drag selection against detached widgets.

    Textual removes widgets (for example when ``Markdown.update()`` replaces blocks
    inside ``batch_update()``) before the compositor reflows, leaving detached nodes
    visible to hit-testing with ``parent is None``. Its ``_watch__select_state``
    also fails to invoke ``SelectState.is_attached_to_dom()`` and ignores detached
    ``content_widget`` endpoints.
    """

    def _is_live_widget(self, widget: Widget | None) -> bool:
        if widget is None or not widget.is_attached:
            return False
        node: Widget | None = widget
        while node is not None:
            if not node.display:
                return False
            if node is self:
                return True
            parent = node.parent
            node = parent if isinstance(parent, Widget) else None
        return False

    def _select_state_is_live(self, select_state: SelectState | None) -> bool:
        if select_state is None:
            return True
        for endpoint in (select_state.start, select_state.end):
            if endpoint is None:
                continue
            if not self._is_live_widget(endpoint.container):
                return False
            if endpoint.content_widget is not None and not self._is_live_widget(endpoint.content_widget):
                return False
        return True

    def _clear_stale_selection(self) -> bool:
        if self._select_state is not None and not self._select_state_is_live(self._select_state):
            self.clear_selection()
            self._stop_auto_scroll()
            return True
        return False

    def _cancel_selection_gesture(self) -> None:
        if self._selecting or self._mouse_down_offset is not None:
            self.clear_selection()
            self._mouse_down_offset = None
        self._stop_auto_scroll()

    def get_widgets_at(self, x: int, y: int) -> Iterable[tuple[Widget, Region]]:
        for widget, region in super().get_widgets_at(x, int(y)):
            if self._is_live_widget(widget):
                yield widget, region

    def get_widget_at(self, x: int, y: int) -> tuple[Widget, Region]:
        widget, region = super().get_widget_at(x, y)
        if self._is_live_widget(widget):
            return widget, region
        for candidate, candidate_region in self.get_widgets_at(x, int(y)):
            return candidate, candidate_region
        raise NoWidget(f"No widget under screen coordinate ({x}, {y})")

    def get_style_at(self, x: int, y: int) -> Style:
        try:
            widget, _ = super().get_widget_at(x, y)
        except NoWidget:
            return Style.null()
        if not self._is_live_widget(widget):
            return Style.null()
        try:
            return super().get_style_at(x, y)
        except NoScreen:
            return Style.null()

    def get_widget_and_offset_at(self, x: int, y: int) -> tuple[Widget | None, Offset | None]:
        try:
            top_widget, _ = super().get_widget_at(x, y)
        except NoWidget:
            pass
        else:
            if not self._is_live_widget(top_widget):
                return None, None
        try:
            widget, offset = super().get_widget_and_offset_at(x, y)
        except NoScreen:
            return None, None
        if widget is None or not self._is_live_widget(widget):
            return None, None
        if offset is not None and widget is not self and not isinstance(widget.parent, Widget):
            return None, None
        return widget, offset

    def _forward_event(self, event: Event) -> None:
        if not event.is_forwarded and isinstance(event, MouseEvent):
            self._clear_stale_selection()
        super()._forward_event(event)

    def _update_select(self) -> None:
        if not self.is_active:
            self._cancel_selection_gesture()
            return
        if self._clear_stale_selection():
            return
        super()._update_select()

    def _watch__select_state(self, select_state: SelectState | None) -> None:
        if select_state is not None and not self._select_state_is_live(select_state):
            self.clear_selection()
            self._stop_auto_scroll()
            return
        super()._watch__select_state(select_state)

    def on_screen_suspend(self) -> None:
        self._cancel_selection_gesture()


class SelectionScreen(SelectionScreenMixin, Screen):
    """Default screen with stale-widget guards for mouse selection."""


class SelectionCopyMixin:
    """Add to a Textual App before App in the inheritance list."""

    def get_default_screen(self) -> Screen:
        return SelectionScreen(id="_default")

    def on_click(self, event: Click) -> None:
        # Double/triple clicks select text after MouseUp's TextSelected event,
        # without creating a drag-selection state. The selection is ready once
        # Click bubbles here from the widget's default handler.
        widget = event.widget
        if (
            event.button != 1
            or event.chain not in (2, 3)
            or widget is None
            or not widget.is_attached
            or isinstance(widget, (Input, TextArea))
            or not widget.allow_select
            or not self.screen.allow_select
            or not self.ALLOW_SELECT
        ):
            return
        if text := self.screen.get_selected_text():
            self.copy_to_clipboard(text)

    def on_text_selected(self, event: TextSelected) -> None:
        # Input (including password fields) and TextArea own their selections;
        # TextSelected covers rendered conversation text and other static text.
        # Textual also emits this event after scrollbar clicks, retaining the
        # old selection. Only copy once for each new text-selection gesture.
        state = self.screen._select_state
        if (
            state is None
            or state.end is None
            or state.start is getattr(self, "_copied_selection_start", None)
        ):
            return
        text = self.screen.get_selected_text()
        if text:
            self._copied_selection_start = state.start
            self.copy_to_clipboard(text)

    def copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        if not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")):
            try:
                copied = _copy_locally(text)
            except (ImportError, OSError, subprocess.SubprocessError):
                super().copy_to_clipboard(text)
                return
            if copied:
                self._clipboard = text
                self.notify(tr(self, "ui.copied"), timeout=1.2, markup=False)
                return
        super().copy_to_clipboard(text)
        if self._driver is not None:
            self.notify(tr(self, "ui.copied"), timeout=1.2, markup=False)
