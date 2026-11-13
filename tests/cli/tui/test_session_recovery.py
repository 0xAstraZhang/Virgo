"""A corrupt saved file must remain reachable through the assembled UI.

Failures: the picker drops retained histories or their paused status; the
controller reads corrupt disk before looking up the live store; switching
workspaces discards the owner; a busy pane opens another copy from disk.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from textual.widgets import OptionList

from virgo.ai.providers import faux
from virgo.ai.types import UserMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.controller import RunController
from virgo.cli.tui.sessions import SessionPicker
from virgo.harness.store import SessionStore

from tests.support.persistence import fail_session_write
from tests.support.tui import has_reply, pump, wait_for


def failed_session(make_session, monkeypatch):
    ui = FullscreenUI()
    session = make_session(faux.reply("unsaved answer"), ui=ui, persist=True)
    with fail_session_write(monkeypatch, session.store.path, partial=True, containing="unsaved answer"):
        session.prompt("original question")
    assert session.store.save_error is not None
    ui.drain()  # The controller's initial reset replays this history once.
    return session, ui


def test_controller_reopens_retained_history_before_reading_disk(make_session, monkeypatch):
    session, ui = failed_session(make_session, monkeypatch)
    original, path = session.main, session.store.path
    session.new_session()
    controller = RunController(session, ui)
    controller.start()
    assert controller.open_session(path)
    updates = pump(controller)
    assert session.main is original
    assert any(update.kind == "reset" and any(
        any(block.get("text") == "unsaved answer" for block in entry.get("message", {}).get("content", [])
            if isinstance(block, dict))
        for entry in update.payload
    ) for update in updates)


@pytest.mark.parametrize("destination", ["new", "workspace", "busy"])
def test_picker_reopens_corrupt_in_memory_history(make_session, monkeypatch, tmp_path, destination):
    async def scenario():
        session, ui = failed_session(make_session, monkeypatch)
        original, path = session.main, session.store.path
        release = threading.Event()
        other = tmp_path / "other"
        other.mkdir()
        saved = SessionStore.new(session.paths.user / "sessions" / "other", other)
        saved.append_message(UserMessage("other workspace"))
        app = VirgoApp(session, ui)
        async with app.run_test(size=(100, 30)) as pilot:
            try:
                await wait_for(pilot, lambda: has_reply(app, "unsaved answer"))
                await pilot.press("ctrl+r")
                await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
                if destination == "workspace":
                    app.screen.dismiss(saved.path)
                else:
                    await pilot.press("n")
                await wait_for(pilot, lambda: app.session.store.path != path and not app.controller.busy)
                if destination == "busy":
                    app.session.commands.register("hold", lambda *_: release.wait(10))
                    app.controller.submit("/hold")
                    await wait_for(pilot, lambda: app.controller.busy)
                await pilot.press("ctrl+r")
                await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
                choices = app.screen.query_one(OptionList)
                assert str(path) in app.screen._rows
                await pilot.pause()
                assert app.screen._rows[str(path)]["status"] == "Unsaved — paused"
                choices.highlighted = choices.get_option_index(str(path))
                await pilot.press("enter")
                await wait_for(pilot, lambda: app.session.main is original and has_reply(app, "unsaved answer"))
                assert app.session_browser_status() == "Unsaved — paused"
            finally:
                release.set()

    asyncio.run(scenario())
