"""Session browser entry points and switching through the assembled TUI."""

from __future__ import annotations

import asyncio

import pytest

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.dialogs import SessionPicker
from virgo.harness.store import SessionStore

from .test_tui_app import has_reply, wait_for


@pytest.mark.parametrize("entry", ["/sessions", "/resume", "ctrl+r"])
def test_browser_entry_cancel_preserves_session_and_remembers_grouping(make_session, entry):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        original = session.store
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            if entry == "ctrl+r":
                app.editor.load_text("draft stays here")
                await pilot.press(entry)
            else:
                app.editor.load_text(entry)
                await pilot.press("enter")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert len(app.screen.sessions) == 1
            assert app.screen.sessions[0]["path"] == original.path
            await pilot.press("g", "escape")
            assert session.store is original and not app.controller.closing
            assert app.editor.text == ("draft stays here" if entry == "ctrl+r" else "")
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert not app.screen.grouped
            await pilot.press("n")
            await wait_for(pilot, lambda: session.store is not original and not app.controller.busy)
            assert session.cwd == original.cwd
            assert not session.main.messages

    asyncio.run(scenario())


def test_browser_waits_for_running_turn(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("finished", delay=0.3), ui=ui, persist=True)
        app = VirgoApp(session, ui, initial="running")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: session.main.agent.running)
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert not app.controller.busy
            assert has_reply(app, "finished")
            assert app.screen.sessions[0]["title"] == "running"
            await pilot.press("escape")
            assert not app.controller.browsing

    asyncio.run(scenario())


def test_browser_opens_other_workspace_and_updates_directory(make_session, tmp_path):
    other = tmp_path / "other-workspace"
    other.mkdir()
    saved = make_session(faux.reply("other answer"), persist=True, cwd=other)
    saved.prompt("other question")
    path = saved.store.path
    saved.shutdown()

    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert {row["cwd"] for row in app.screen.sessions} == {session.cwd, other}
            app.screen.dismiss(path)
            await wait_for(pilot, lambda: app.session.cwd == other and has_reply(app, "other answer") and not app.controller.busy)
            assert app.session is app.controller.session
            assert app.session.store.path == path
            assert has_reply(app, "other answer")
            assert "other-workspace" in str(app.screen_stack[0].query_one("#cwd").content)

    asyncio.run(scenario())


def test_shutdown_discards_only_visited_empty_sessions(make_session):
    session = make_session(persist=True)
    session.set_model("faux/changed")
    abandoned = session.store.path
    session.new_session()
    session.set_model("faux/changed")
    current = session.store.path
    untouched = SessionStore.new(session.paths.sessions, session.cwd)
    untouched.append("model_change", model="faux/untouched")
    session.shutdown()
    assert not abandoned.exists()
    assert not current.exists()
    assert untouched.path.exists()


def test_shutdown_preserves_conversation_and_extension_state(make_session):
    session = make_session(faux.reply("keep this"), persist=True)
    session.prompt("saved message")
    conversation = session.store.path
    session.new_session()
    session.store.append("custom", kind="goal", data={"target": "finish the task"})
    state = session.store.path
    session.shutdown()
    assert conversation.exists() and state.exists()


def test_shutdown_hook_state_is_checked_before_empty_cleanup(make_session):
    session = make_session(persist=True)
    session.bus.on("session_shutdown", lambda s: s.store.append("custom", kind="extension", data="saved"))
    session.shutdown()
    assert session.store.path.exists()


def test_invalid_history_keeps_previous_store_and_runtime(make_session):
    session = make_session(faux.reply("saved answer"), persist=True)
    session.prompt("saved question")
    previous, runtime = session.store, session.main
    broken = SessionStore.new(session.paths.sessions, session.cwd)
    broken.append("message", message={"role": "invalid", "content": "broken"})
    with pytest.raises(ValueError):
        session.open_session(broken.path)
    assert session.store is previous and session.main is runtime
    assert session.main.store is previous


def test_shutdown_prevents_restarting_a_saved_or_new_session(make_session):
    session = make_session(faux.reply("saved answer"), persist=True)
    session.prompt("saved question")
    previous = session.store
    starts = []
    session.bus.on("session_start", lambda _: starts.append(True))
    session.shutdown()
    session.open_session(previous.path)
    session.new_session()
    assert session.store is previous and not starts
