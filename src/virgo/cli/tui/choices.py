"""Compact choices shared by slash completion and inline login prompts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from textual import on
from textual.widgets import OptionList
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class CommandChoice:
    """Two independent columns, so description continuations retain their indent."""

    command: str
    description: str

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(1, options.max_width)
        command_width = min(18, max(1, (width - 1) // 3))
        columns = Table.grid(padding=(0, 1), expand=True)
        columns.add_column(width=command_width, overflow="fold")
        columns.add_column(ratio=1, overflow="fold")
        columns.add_row(
            Text(self.command, overflow="fold"),
            Text(self.description.replace("\n", " "), overflow="fold"),
        )
        yield columns


class ChoiceList(OptionList):
    """Wrap choices to the available width, with selection driving immediate scrolling."""

    DEFAULT_CSS = """
    ChoiceList { text-wrap: wrap; text-overflow: fold; scrollbar-size: 0 0; }
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
            if isinstance(prompt, CommandChoice):
                choices.append(option)
                continue
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


class MarkedChoiceList(ChoiceList):
    """Show a cursor beside the highlighted choice in a short dock menu."""

    def set_choices(self, rows: list[tuple[str, str | Text]], highlighted: int) -> None:
        self._rows = rows
        self.set_options(
            Option(self._row(index, highlighted), id=choice_id)
            for index, (choice_id, _) in enumerate(rows)
        )
        self.highlighted = highlighted

    def _row(self, index: int, highlighted: int) -> Text:
        return Text.assemble("› " if index == highlighted else "  ", self._rows[index][1])

    @on(OptionList.OptionHighlighted)
    def update_marker(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option_index == self.highlighted:
            for index in range(self.option_count):
                self.replace_option_prompt_at_index(index, self._row(index, self.highlighted))
