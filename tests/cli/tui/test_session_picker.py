"""Sorting, keyboard navigation, and terminal layout for the session browser."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console
from textual import on
from textual.app import App
from textual.widgets import OptionList, Static

from virgo.cli.tui.app import VirgoApp, terminal_theme
from virgo.cli.tui.sessions import SessionPicker, SessionRow, relative_time


class PickerApp(App):
    CSS = VirgoApp.CSS

    def __init__(self, picker):
        super().__init__(ansi_color=True)
        self.picker = picker
        self.result = "pending"
        self.deletions = []
        self.register_theme(terminal_theme())
        self.theme = "virgo-terminal"

    def on_mount(self):
        self.push_screen(self.picker, self.picked)

    def picked(self, result):
        self.result = result

    @on(SessionPicker.DeleteRequested)
    def delete_requested(self, event):
        self.deletions.append(event.path)


def saved(path, cwd, updated, created):
    return {
        "path": Path(path), "cwd": Path(cwd), "mtime": updated,
        "workspace_created_at": created, "title": f"会话 {path}", "messages": 2,
    }


def row_ids(picker):
    return [option.id for option in picker.query_one(OptionList).options if option.id]


def test_grouping_sorts_by_workspace_creation_then_update_and_keeps_selection():
    async def scenario():
        picker = SessionPicker([
            saved("/old/first", "/old", 400, 10),
            saved("/new/first", "/new", 100, 20),
            saved("/new/second", "/new", 300, 20),
        ])
        app = PickerApp(picker)
        async with app.run_test(size=(80, 24)) as pilot:
            assert row_ids(picker) == [str(Path(p)) for p in ("/new/second", "/new/first", "/old/first")]
            highlight = picker.query_one(OptionList).get_component_rich_style("option-list--option-highlighted")
            assert highlight.color.triplet == (255, 255, 255)
            assert highlight.bgcolor.triplet == (124, 58, 237)
            assert highlight.bold and not highlight.reverse
            assert picker.selected_path == Path("/new/second")
            await pilot.press("down")
            assert picker.selected_path == Path("/new/first")
            await pilot.press("g")
            assert not picker.grouped
            assert row_ids(picker) == [str(Path(p)) for p in ("/old/first", "/new/second", "/new/first")]
            assert not any(option.disabled for option in picker.query_one(OptionList).options)
            assert picker.selected_path == Path("/new/first")
            await pilot.press("g")
            assert picker.grouped
            assert row_ids(picker) == [str(Path(p)) for p in ("/new/second", "/new/first", "/old/first")]
            assert picker.selected_path == Path("/new/first")
            await pilot.press("down", "enter")
            assert app.result == Path("/old/first")

    asyncio.run(scenario())


@pytest.mark.parametrize(("key", "result"), [("escape", None), ("n", "new")])
@pytest.mark.parametrize("empty", [False, True])
def test_cancel_and_new_work_with_or_without_saved_sessions(key, result, empty):
    async def scenario():
        picker = SessionPicker([] if empty else [saved("/one", "/workspace", 100, 10)])
        app = PickerApp(picker)
        async with app.run_test(size=(80, 24)) as pilot:
            assert picker.query_one("#session-empty").display == empty
            await pilot.press("g", key)
            assert app.result == result

    asyncio.run(scenario())


@pytest.mark.parametrize("grouped", [False, True])
def test_delete_requests_selected_path_and_success_selects_adjacent_session(grouped):
    async def scenario():
        picker = SessionPicker([
            saved("/old/first", "/old", 400, 10),
            saved("/new/first", "/new", 100, 20),
            saved("/new/second", "/new", 300, 20),
        ], grouped=grouped)
        app = PickerApp(picker)
        async with app.run_test(size=(80, 24)) as pilot:
            original = row_ids(picker)
            await pilot.press("down", "ctrl+x")
            deleted = Path(original[1])
            assert app.deletions == [deleted]
            assert picker.selected_path == deleted
            assert row_ids(picker) == original
            assert app.result == "pending"

            picker.remove_session(deleted)
            await pilot.pause()
            assert row_ids(picker) == [original[0], original[2]]
            assert picker.selected_path == Path(original[2])
            assert not picker.query("#session-heading, #session-summary")
            assert picker.grouped == grouped
            assert picker.query_one(OptionList).get_option(original[2]).prompt.selected

            picker.remove_session(picker.selected_path)
            await pilot.pause()
            assert picker.selected_path == Path(original[0])
            assert not picker.query("#session-heading, #session-summary")
            assert len(picker.query_one(OptionList).options) == (2 if grouped else 1)

            picker.remove_session(picker.selected_path)
            await pilot.pause()
            assert picker.selected_path is None
            assert row_ids(picker) == []
            assert not picker.query("#session-heading, #session-summary")
            assert picker.query_one("#session-empty").display
            assert not picker.query_one("#session-list").display
            assert not picker.query_one("#session-columns").display
            await pilot.press("ctrl+x")
            assert app.deletions == [deleted]
            await pilot.press("g", "n")
            assert app.result == "new"

    asyncio.run(scenario())


def test_removing_another_session_preserves_selection():
    async def scenario():
        picker = SessionPicker([
            saved("/first", "/workspace", 200, 10),
            saved("/second", "/workspace", 100, 10),
        ])
        app = PickerApp(picker)
        async with app.run_test(size=(80, 24)) as pilot:
            picker.remove_session(Path("/second"))
            await pilot.pause()
            assert picker.selected_path == Path("/first")

    asyncio.run(scenario())


def test_paging_resize_and_chinese_title_stay_within_browser():
    async def scenario():
        rows = [saved(f"/workspace/{i}", "/workspace", i, 10) for i in range(60)]
        rows[0]["title"] = "查看中文会话" * 50 + "\n第二行"
        picker = SessionPicker(rows, current_path=Path("/workspace/0"))
        app = PickerApp(picker)
        async with app.run_test(size=(80, 24)) as pilot:
            choices = picker.query_one(OptionList)
            assert choices.region.height >= 14
            await pilot.press("pagedown")
            assert picker.selected_path != Path("/workspace/59")
            await pilot.press("end")
            assert picker.selected_path == Path("/workspace/0")
            assert choices.scroll_y > 0
            option = choices.get_option(str(Path("/workspace/0")))
            assert option.prompt.status == "Ready"
            assert option.prompt.selected
            await pilot.resize_terminal(36, 14)
            await pilot.pause()
            assert picker.selected_path == Path("/workspace/0")
            assert choices.region.right <= 36
            footer = picker.query_one("#session-footer", Static)
            assert footer.region.bottom <= 14
            assert choices.region.bottom <= footer.region.y
            assert "Ctrl+X Move to trash" in str(footer.content)
            console = Console(width=footer.content_region.width)
            assert len(console.render_lines(footer.content, console.options)) <= footer.content_region.height
            await pilot.press("home")
            assert picker.selected_path == Path("/workspace/59")
            await pilot.press("pageup")
            assert picker.selected_path == Path("/workspace/59")

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [24, 36, 80])
def test_row_truncates_by_terminal_cells_without_wrapping(width):
    console = Console(width=width, color_system=None)
    row = SessionRow("中文" * 100 + "\nnext", "Saved", "10d ago", selected=True)
    lines = console.render_lines(row, console.options)
    assert len(lines) == 1
    assert sum(segment.cell_length for segment in lines[0]) == width
    assert "› ○ " in "".join(segment.text for segment in lines[0])


def test_relative_times():
    assert relative_time(200, now=100) == "now"
    assert relative_time(90, now=100) == "now"
    assert relative_time(30, now=100) == "1m ago"
    assert relative_time(0, now=7200) == "2h ago"
    assert relative_time(0, now=172800) == "2d ago"
