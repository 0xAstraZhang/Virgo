from __future__ import annotations

import asyncio

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI

from .test_tui_app import wait_for


def test_enter_uses_highlighted_command_when_typed_name_is_also_a_match(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        calls = []
        session.commands.register("model-more", lambda s, a: calls.append("model-more"))
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("/model")
            await wait_for(pilot, lambda: app.completion_names == ["/model", "/model-more"])
            await pilot.press("down", "enter")
            assert app.editor.text == "/model-more "
            assert not calls and not session.main.messages
            await pilot.press("enter")
            await wait_for(pilot, lambda: calls == ["model-more"] and not app.controller.busy)
            # Modified arrows still select multiline editor text.
            app.editor.load_text("first\nsecond")
            app.editor.move_cursor(app.editor.document.end)
            await pilot.press("shift+up")
            assert app.editor.selection.start != app.editor.selection.end
            assert app.editor.text == "first\nsecond"

    asyncio.run(scenario())


def test_fast_submit_refreshes_completion_matches_before_accepting(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        calls = []
        session.commands.register("picked", lambda s, a: calls.append("picked"))
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("/")
            await wait_for(pilot, lambda: len(app.completion_names) > 1)
            app.editor.load_text("/picked")
            # Enter before the queued TextArea.Changed handler has run.
            app.editor.action_submit()
            await wait_for(pilot, lambda: calls == ["picked"] and not app.controller.busy)
            assert app.editor.text == ""
            assert not session.main.messages

    asyncio.run(scenario())
