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
            selected_top = choices._index_to_line[count - 1]
            selected_bottom = selected_top + choices._heights[count - 1]
            assert choices.scroll_offset.y <= selected_top
            assert selected_bottom <= choices.scroll_offset.y + choices.scrollable_content_region.height
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


def test_filter_resets_selection_after_a_long_wrapped_command(make_session):
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
            assert choices.virtual_size.height > choices.option_count
            assert choices._heights[choices.highlighted] > 1
            app.editor.load_text("/mo")
            app.editor.move_cursor(app.editor.document.end)
            await wait_for(pilot, lambda: app.completion_names == ["/model"])
            assert choices.highlighted == 0 and choices.scroll_y == 0
            await pilot.press("tab")
            assert app.editor.text == "/model "
            assert not choices.display

    asyncio.run(scenario())


def test_login_description_wraps_fully_and_reflows_when_window_resizes(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        expected = "/login " + session.commands.get("login").description
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("/login")
            await wait_for(pilot, lambda: app.completion_names == ["/login"])
            choices = app.query_one("#completions", OptionList)
            heights = {}
            layouts = {}
            for width in (80, 52, 120, 80):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                assert choices.highlighted == 0 and app.focused is app.editor
                lines = [choices.render_line(y).text for y in range(choices.scrollable_content_region.height)]
                rendered = " ".join(" ".join(lines).split())
                assert rendered == " ".join(expected.split())
                assert "…" not in rendered
                heights[width] = choices.virtual_size.height
                layouts[width] = [line.rstrip() for line in lines if line.strip()]
                description_column = layouts[width][0].index("sign in")
                assert all(not line[:description_column].strip() for line in layouts[width][1:])
                assert choices.region.right <= width
                assert choices.region.bottom <= app.editor.region.y
                assert app.editor.region.bottom <= app.query_one("#status").region.y
                assert app.query_one("#status").region.bottom <= 24
            assert layouts[52] != layouts[80]
            assert heights[52] >= heights[80] > heights[120]
            assert heights[120] == 1

    asyncio.run(scenario())


def test_long_command_and_chinese_description_keep_separate_columns(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        command = "a-very-long-custom-command-name"
        description = "说明文字会按介绍栏的宽度换行，不会跑到命令名称下面。"
        session.commands.register(command, lambda s, a: None, description)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 30)) as pilot:
            app.editor.load_text("/a-very")
            await wait_for(pilot, lambda: app.completion_names == [f"/{command}"])
            choices = app.query_one("#completions", OptionList)
            for width in (80, 40, 120):
                await pilot.resize_terminal(width, 30)
                await pilot.pause()
                option = choices.get_option_at_index(0)
                strips = choices._get_option_render(
                    option, choices.get_visual_style("option-list--option-highlighted")
                )
                lines = [strip.text for strip in strips]
                column = lines[0].index("说明")
                assert "".join(line[:column].strip() for line in lines) == f"/{command}"
                assert "".join(line[column:].strip() for line in lines) == description
                assert all(strip.cell_length <= choices.scrollable_content_region.width for strip in strips)
                assert choices.styles.scrollbar_size_vertical == 0
                assert choices.styles.scrollbar_size_horizontal == 0
            await pilot.press("tab")
            assert app.editor.text == f"/{command} "

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
