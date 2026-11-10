"""Compact choices shared by slash completion and inline login prompts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Self

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option


class ChoiceList(OptionList):
    """Wrap choices to the available width, with selection driving immediate scrolling."""

    DEFAULT_CSS = """
    ChoiceList { text-wrap: wrap; text-overflow: fold; }
    ChoiceList > .option-list--option-highlighted,
    ChoiceList:focus > .option-list--option-highlighted {
        background: #7C3AED;
        color: #FFFFFF;
        text-style: bold;
    }
    """

    def add_options(self, new_options: Iterable[Option | Text | str | None]) -> Self:
        choices = []
        for item in new_options:
            if item is None:
                choices.append(None)
                continue
            option = item if isinstance(item, Option) else Option(item)
            prompt = option.prompt
            if isinstance(prompt, Text):
                text = prompt.copy()
                if "\n" in text.plain:
                    text = Text(text.plain.replace("\n", " "), style=text.style)
            else:
                text = Text(str(prompt).replace("\n", " "))
            text.no_wrap = False
            text.overflow = "fold"
            choices.append(Option(text, id=option.id, disabled=option.disabled))
        super().add_options(choices)
        if self.is_mounted and self.highlighted is None and self.option_count:
            self.action_first()
        return self

    def on_resize(self) -> None:
        self.call_after_refresh(self.scroll_to_highlight)

    def move_selection(self, direction: int) -> bool:
        if not self.display or not self.option_count:
            return False
        if direction < 0:
            self.action_cursor_up()
        else:
            self.action_cursor_down()
        # OptionList scrolls the selected row with animate=False and immediate=True.
        # Calling it even at a clamped boundary also keeps selection visible after resize.
        self.scroll_to_highlight()
        return True
