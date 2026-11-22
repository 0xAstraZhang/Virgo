"""Copy completed mouse selections to the local clipboard or terminal."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from textual.events import Click, TextSelected
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


class SelectionCopyMixin:
    """Add to a Textual App before App in the inheritance list."""

    def on_click(self, event: Click) -> None:
        # Double/triple clicks select text after MouseUp's TextSelected event,
        # without creating a drag-selection state. The selection is ready once
        # Click bubbles here from the widget's default handler.
        widget = event.widget
        if (
            event.button != 1
            or event.chain not in (2, 3)
            or widget is None
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
