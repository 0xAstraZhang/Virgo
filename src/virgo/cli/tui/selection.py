"""Copy completed mouse selections, including macOS Terminal's system clipboard."""

from __future__ import annotations

import os
import subprocess
import sys

from textual.events import TextSelected


class SelectionCopyMixin:
    """Add to a Textual App before App in the inheritance list."""

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
        if sys.platform == "darwin" and not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")):
            self._clipboard = text
            try:
                subprocess.run(
                    ["/usr/bin/pbcopy"],
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=True,
                )
                self.notify("Copied", timeout=1.2, markup=False)
                return
            except (OSError, subprocess.SubprocessError):
                # Keep the existing fallback, but don't report a successful
                # system copy after pbcopy failed (Terminal.app ignores OSC52).
                super().copy_to_clipboard(text)
                return
        # Keep Textual's internal clipboard and OSC52 support on other hosts.
        super().copy_to_clipboard(text)
        if self._driver is not None:
            self.notify("Copied", timeout=1.2, markup=False)
