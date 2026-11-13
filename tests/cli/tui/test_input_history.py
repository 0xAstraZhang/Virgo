"""Navigate input history through the full app's keyboard and completion handling."""

import asyncio

import pytest

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI


@pytest.mark.parametrize("previous,next_", [("up", "down"), ("ctrl+p", "ctrl+n")])
def test_history_boundaries_and_editing_recalled_command(make_session, previous, next_):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        app.history.add("/login")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(previous, previous)
            assert app.editor.text == "/login"
            assert not app.query_one("#completions").display
            await pilot.press(next_, next_)
            assert app.editor.text == ""
            await pilot.press(previous, "backspace")
            assert app.editor.text == "/logi"
            assert app.query_one("#completions").display
            await pilot.press("tab")
            assert app.editor.text == "/login "

    asyncio.run(scenario())
