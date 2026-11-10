"""Multiline chat editor, with command completion and persistent input history."""

from __future__ import annotations

import json
from pathlib import Path

from textual.binding import Binding
from textual.events import Key, MouseUp
from textual.message import Message
from textual.widgets import TextArea

from .choices import ChoiceList


class InputHistory:
    def __init__(self, directory: Path):
        self.path = directory / "tui-history.jsonl"
        self.entries: list[str] = []
        try:
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, str) and value.strip():
                        self.entries.append(value)
            else:
                self.entries = (directory / "history").read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
        self.entries = self.entries[-1000:]
        self.index = len(self.entries)
        self.draft = ""

    def add(self, text: str) -> None:
        if not self.entries or self.entries[-1] != text:
            self.entries.append(text)
            self.entries = self.entries[-1000:]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self.entries),
                    encoding="utf-8",
                )
            except OSError:
                pass
        self.reset()

    def reset(self) -> None:
        self.index = len(self.entries)
        self.draft = ""

    def move(self, direction: int, current: str) -> str:
        if self.index == len(self.entries):
            self.draft = current
        self.index = max(0, min(len(self.entries), self.index + direction))
        return self.draft if self.index == len(self.entries) else self.entries[self.index]


class ChatEditor(TextArea):
    BINDINGS = [
        Binding("enter", "submit", show=False),
        Binding("shift+enter", "newline", show=False),
        # LF remains compatible with terminal key mappings and pasted key input.
        Binding("ctrl+j", "newline", show=False),
        Binding("tab", "complete", show=False, priority=True),
        Binding("up", "cursor_up", show=False, priority=True),
        Binding("down", "cursor_down", show=False, priority=True),
        Binding("ctrl+p", "history(-1)", show=False),
        Binding("ctrl+n", "history(1)", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str):
            self.text = text
            super().__init__()

    def __init__(self, history: InputHistory):
        super().__init__(
            id="editor",
            soft_wrap=True,
            show_line_numbers=False,
            highlight_cursor_line=False,
            theme="css",
        )
        self.input_history = history

    def on_key(self, event: Key) -> None:
        # Handle these in the editor queue, after preceding typed characters.
        # Priority bindings run in the app queue and can overtake text inserts.
        if event.key in {"enter", "shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            if event.key == "enter":
                self.action_submit()
            else:
                self.action_newline()

    def action_submit(self) -> None:
        self.app.sync_completions()
        names = self.app.completion_names
        if names:
            choices = self.app.screen_stack[0].query_one("#completions", ChoiceList)
            selected = names[choices.highlighted or 0]
            if self.text != selected and self.app.accept_completion():
                return
        text = self.text.strip()
        if text:
            self.input_history.add(text)
            self.clear()
            self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")

    def on_mouse_up(self, event: MouseUp) -> None:
        if event.button == 1 and self.selected_text:
            self.app.copy_to_clipboard(self.selected_text)

    def action_complete(self) -> None:
        if not self.app.accept_completion():
            self.screen.focus_next()

    def action_history(self, direction: int) -> None:
        self.load_text(self.input_history.move(direction, self.text))
        self.move_cursor(self.document.end)

    def action_cursor_up(self, select: bool = False) -> None:
        if not select and self.app.move_completion(-1):
            return
        visual_row = self.wrapped_document.location_to_offset(self.cursor_location).y
        if not select and visual_row == 0:
            self.action_history(-1)
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if not select and self.app.move_completion(1):
            return
        visual_row = self.wrapped_document.location_to_offset(self.cursor_location).y
        if not select and visual_row == self.wrapped_document.height - 1:
            self.action_history(1)
        else:
            super().action_cursor_down(select)
