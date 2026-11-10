"""Fullscreen browser for saved sessions across workspaces."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


def relative_time(timestamp: float, *, now: float | None = None) -> str:
    seconds = max(0, int((time.time() if now is None else now) - timestamp))
    if seconds < 60:
        return "now"
    for duration, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= duration:
            return f"{seconds // duration}{unit} ago"
    return "now"


def workspace_name(cwd: str | Path) -> str:
    value = str(cwd)
    home = str(Path.home())
    if value == home or value.startswith(home + "/"):
        value = "~" + value[len(home) :]
    return " ".join(value.split()) or "Unknown workspace"


def _columns(width: int) -> tuple[int, int, int]:
    """Keep useful title space on narrow terminals; dates remain visible."""
    updated = min(10, max(1, width // 4))
    status = 9 if width >= 48 else 0
    return max(1, width - updated - status - (4 if status else 2)), status, updated


def _cell(value: str, width: int, *, right: bool = False) -> Text:
    cell = Text(value.replace("\n", " ").replace("\r", " ").replace("\t", " "), no_wrap=True, overflow="ellipsis")
    cell.truncate(width, overflow="ellipsis", pad=not right)
    if right:
        cell.pad_left(max(0, width - cell.cell_len))
    return cell


@dataclass(frozen=True)
class SessionRow:
    title: str
    status: str
    updated: str
    selected: bool = False
    heading: bool = False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        title_width, status_width, updated_width = _columns(max(1, options.max_width))
        prefix = "  " if self.heading else ("› ○ " if self.selected else "  ○ ")
        line = _cell(prefix + self.title, title_width)
        if status_width:
            line.append("  ")
            line.append_text(_cell(self.status, status_width))
        line.append("  ")
        line.append_text(_cell(self.updated, updated_width, right=True))
        yield line


class SessionList(OptionList):
    """Keep page navigation on sessions when a workspace header borders a page."""

    def action_page_up(self) -> None:
        super().action_page_up()
        if self.highlighted is None:
            self.action_first()

    def action_page_down(self) -> None:
        super().action_page_down()
        if self.highlighted is None:
            self.action_last()

    def on_resize(self) -> None:
        self.call_after_refresh(self.scroll_to_highlight)


class SessionPicker(ModalScreen[Path | str | None]):
    """`g` changes grouping; dismisses a path, ``"new"``, or None."""

    CSS = """
    SessionPicker { align: left top; background: $background; color: $foreground; }
    SessionPicker #session-browser { width: 100%; height: 100%; padding: 1 2; }
    SessionPicker #session-heading { height: 1; }
    SessionPicker #session-summary { height: 2; border-bottom: solid $foreground; }
    SessionPicker #session-columns { height: 1; margin-top: 1; text-style: dim; }
    SessionPicker #session-list { height: 1fr; max-height: 100%; border: none; padding: 0;
        margin: 0; background: $background; scrollbar-size: 0 0; }
    SessionPicker #session-list:focus { border: none; background-tint: transparent; }
    SessionPicker #session-list > .option-list--option { padding: 0; text-style: dim; }
    SessionPicker #session-list > .option-list--option-disabled { color: $foreground; text-style: dim; }
    SessionPicker #session-list > .option-list--option-highlighted,
    SessionPicker #session-list:focus > .option-list--option-highlighted {
        background: #AFD7FF; color: #000000; text-style: bold not dim;
    }
    SessionPicker #session-empty { height: 1fr; padding-top: 1; text-style: dim; }
    SessionPicker #session-footer { height: auto; max-height: 2; margin-top: 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("g", "toggle_group", show=False),
        Binding("n", "new", show=False),
    ]

    def __init__(
        self,
        sessions: list[dict],
        *,
        current_path: Path | None = None,
        grouped: bool = True,
        current_status: str = "Ready",
    ):
        super().__init__()
        self.sessions = sessions
        self.current_path = Path(current_path) if current_path is not None else None
        self.grouped = grouped
        self.current_status = current_status
        self._rows: dict[str, dict] = {}
        self._selected_id: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="session-browser"):
            yield Static(id="session-heading")
            yield Static(Text.assemble((f"All {len(self.sessions)}", "bold")), id="session-summary")
            yield Static(SessionRow("Session", "Status", "Updated", heading=True), id="session-columns")
            yield SessionList(id="session-list")
            yield Static("No saved sessions. Press n to start a new session.", id="session-empty", markup=False)
            yield Static(
                Text.assemble(
                    ("Esc", "bold"), " back  ", ("↑/↓", "bold"), " move  ",
                    ("Enter", "bold"), " open  ", ("g", "bold"), " group  ", ("n", "bold"), " new",
                ),
                id="session-footer",
            )

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#session-list", OptionList).focus()

    def _row(self, item: dict, *, selected: bool = False) -> SessionRow:
        current = self.current_path is not None and Path(item["path"]) == self.current_path
        return SessionRow(
            (item.get("title") if item.get("title") != "(empty)" else None) or "Untitled session",
            self.current_status if current else "Saved",
            relative_time(item.get("mtime", time.time())),
            selected=selected,
        )

    def _rebuild(self, selected_path: Path | None = None) -> None:
        choices = self.query_one("#session-list", OptionList)
        choices.clear_options()
        self._selected_id = None
        self._rows = {}
        rows = sorted(self.sessions, key=lambda item: (-item.get("mtime", 0), str(item["path"])))
        if self.grouped:
            workspaces: dict[str, list[dict]] = defaultdict(list)
            for item in rows:
                workspaces[str(item.get("cwd", ""))].append(item)
            groups = sorted(
                workspaces.items(),
                key=lambda pair: (-max(item.get("workspace_created_at", 0) for item in pair[1]), pair[0]),
            )
        else:
            groups = [("", rows)]
        options = []
        for number, (cwd, items) in enumerate(groups):
            if self.grouped:
                if number:
                    options.append(Option(Text(""), disabled=True))
                options.append(Option(Text(f"{workspace_name(cwd)}  {len(items)}", no_wrap=True, overflow="ellipsis"), disabled=True))
            for item in items:
                key = str(item["path"])
                self._rows[key] = item
                options.append(Option(self._row(item), id=key))
        choices.add_options(options)
        choices.display = bool(rows)
        self.query_one("#session-empty").display = not rows
        self.query_one("#session-columns").display = bool(rows)
        self.query_one("#session-heading", Static).update(
            Text.assemble(("Sessions", "bold"), (f"    Group: {'Workspace' if self.grouped else 'None'}  ", "dim"), ("g", "bold"))
        )
        if selected_path is not None and str(selected_path) in self._rows:
            choices.highlighted = choices.get_option_index(str(selected_path))
        else:
            choices.action_first()
        self._update_selection()

    @property
    def selected_path(self) -> Path | None:
        choices = self.query_one("#session-list", OptionList)
        if choices.highlighted is None:
            return None
        key = choices.get_option_at_index(choices.highlighted).id
        return Path(key) if key in self._rows else None

    def _update_selection(self) -> None:
        choices = self.query_one("#session-list", OptionList)
        path = self.selected_path
        selected_id = str(path) if path is not None else None
        if self._selected_id == selected_id:
            return
        if self._selected_id in self._rows:
            choices.replace_option_prompt(self._selected_id, self._row(self._rows[self._selected_id]))
        if selected_id is not None:
            choices.replace_option_prompt(selected_id, self._row(self._rows[selected_id], selected=True))
        self._selected_id = selected_id

    @on(OptionList.OptionHighlighted, "#session-list")
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._update_selection()

    @on(OptionList.OptionSelected, "#session-list")
    def selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id in self._rows:
            self.dismiss(Path(event.option_id))

    def action_toggle_group(self) -> None:
        path = self.selected_path
        self.grouped = not self.grouped
        self._rebuild(path)

    def action_new(self) -> None:
        self.dismiss("new")

    def action_cancel(self) -> None:
        self.dismiss(None)
