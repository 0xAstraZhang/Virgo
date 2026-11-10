"""The bottom command list selects rows immediately while retaining editor focus."""

from __future__ import annotations

import asyncio

from rich.color import Color
from textual import events
from textual.widgets import OptionList

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI

from .test_tui_app import wait_for


def test_slash_highlights_first_command_with_editor_focused(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("/")
            await wait_for(pilot, lambda: bool(app.completion_names))
            choices = app.query_one("#completions", OptionList)
            assert app.focused is app.editor
            assert choices.highlighted == 0 and choices.scroll_y == 0
            first_styles = [segment.style for segment in choices.render_line(0) if segment.text.strip()]
            second_styles = [segment.style for segment in choices.render_line(1) if segment.text.strip()]
            assert any(
                style is not None and style.bgcolor == Color.parse("#7c3aed")
                and style.color == Color.parse("#ffffff") and style.bold
                for style in first_styles
            )
            assert all(style is None or style.bgcolor != Color.parse("#7c3aed") for style in second_styles)

    asyncio.run(scenario())


def test_repeated_arrows_select_rows_without_changing_editor_or_lagging_scroll(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        app.history.entries = ["previous chat"]
        app.history.reset()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("/")
            await wait_for(pilot, lambda: bool(app.completion_names))
            choices = app.query_one("#completions", OptionList)
            count = len(app.completion_names)
            assert count > choices.scrollable_content_region.height
            await pilot.press("down")
            assert choices.highlighted == 1
            assert app.editor.text == "/" and app.focused is app.editor
            await pilot.press("up")
            assert choices.highlighted == 0
            # A burst models key-repeat events without waiting for any scroll animation.
            for _ in range(count - 1):
                app.post_message(events.Key("down", None))
            await wait_for(pilot, lambda: choices.highlighted == count - 1)
            assert choices.scroll_y == choices.scroll_target_y
            assert choices.scroll_offset.y <= count - 1 < (
                choices.scroll_offset.y + choices.scrollable_content_region.height
            )
            assert app.editor.text == "/" and app.history.index == len(app.history.entries)
            for _ in range(count - 1):
                app.post_message(events.Key("up", None))
            await wait_for(pilot, lambda: choices.highlighted == 0)
            assert choices.scroll_y == choices.scroll_target_y == 0
            assert app.editor.text == "/" and app.focused is app.editor
            await pilot.press("down", "down", "tab")
            assert app.editor.text.endswith(" ")
            assert not choices.display

    asyncio.run(scenario())


def test_filter_resets_selection_and_long_command_descriptions_stay_on_one_row(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        session.commands.register("zlong", lambda s, a: None, "very long description " * 50)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("/")
            await wait_for(pilot, lambda: bool(app.completion_names))
            choices = app.query_one("#completions", OptionList)
            for _ in range(len(app.completion_names) - 1):
                app.post_message(events.Key("down", None))
            await wait_for(pilot, lambda: app.completion_names[choices.highlighted] == "/zlong")
            assert choices.virtual_size.height == choices.option_count
            app.editor.load_text("/mo")
            app.editor.move_cursor(app.editor.document.end)
            await wait_for(pilot, lambda: app.completion_names == ["/model"])
            assert choices.highlighted == 0 and choices.scroll_y == 0
            await pilot.press("tab")
            assert app.editor.text == "/model "
            assert not choices.display

    asyncio.run(scenario())


def test_escape_only_hides_completions_until_the_input_changes(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("/")
            await wait_for(pilot, lambda: bool(app.completion_names))
            await pilot.press("escape")
            assert not app.query_one("#completions").display
            await pilot.press("backspace", "/")
            await wait_for(pilot, lambda: bool(app.completion_names))
            assert app.query_one("#completions").highlighted == 0

    asyncio.run(scenario())
