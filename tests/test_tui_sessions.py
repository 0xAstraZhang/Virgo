"""Session browser entry points and switching through the assembled TUI."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from textual.widgets import Input, OptionList

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.dialogs import SessionPicker
from virgo.harness.store import SessionStore

from .test_tui_app import has_reply, wait_for


@pytest.mark.parametrize("entry", ["/sessions", "ctrl+r"])
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


@pytest.mark.parametrize("previous", ["workspace", "none"])
@pytest.mark.parametrize("exit_key", ["escape", "ctrl+d"])
def test_browser_view_survives_restart_and_preserves_other_settings(make_session, previous, exit_key):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        session.settings.save_user({"effort": "high", "tui": {"sessionGrouping": previous, "other": "keep"}})
        app = VirgoApp(session, ui)
        expected = "none" if previous == "workspace" else "workspace"
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert app.screen.grouped == (previous == "workspace")
            await pilot.press("g")
            saved = json.loads((session.paths.user / "settings.json").read_text())
            assert saved["tui"] == {"sessionGrouping": expected, "other": "keep"}
            assert saved["effort"] == "high"
            await pilot.press(exit_key)
        assert app.controller.wait_closed()

        restarted_ui = FullscreenUI()
        restarted = make_session(ui=restarted_ui)
        reopened = VirgoApp(restarted, restarted_ui, pick_session=True)
        async with reopened.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: isinstance(reopened.screen, SessionPicker))
            assert reopened.screen.grouped == (expected == "workspace")
            await pilot.press("escape")

    asyncio.run(scenario())


def test_browser_remains_usable_when_view_preference_cannot_be_saved(make_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        app = VirgoApp(session, ui)
        notices = []

        def fail(_patch):
            raise OSError("read-only settings")

        monkeypatch.setattr(session.settings, "save_user", fail)
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+r", "g")
            assert not app.screen.grouped
            assert notices and "Cannot save session view" in notices[0]
            await pilot.press("escape", "ctrl+r")
            assert isinstance(app.screen, SessionPicker) and not app.screen.grouped
            await pilot.press("escape")

    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["/sessions", "ctrl+r"])
@pytest.mark.parametrize("work", ["agent", "command", "helper", "queued"])
def test_browser_opens_immediately_while_work_continues(make_session, entry, work):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def wait(*_args):
            entered.set()
            assert release.wait(15)
            return faux.reply("finished")

        ui = FullscreenUI()
        session = make_session(wait, ui=ui, persist=True)
        session.commands.register("waiting", wait, concurrent=work == "helper")
        app = VirgoApp(session, ui, initial="running" if work == "agent" else "/waiting")
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await wait_for(pilot, entered.is_set)
                if work == "queued":
                    app.controller.submit("/effort high")
                app.editor.load_text("draft stays here" if entry == "ctrl+r" else entry)
                await pilot.press("ctrl+r" if entry == "ctrl+r" else "enter")
                await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
                assert not release.is_set()
                assert not app.controller.can_switch
                assert app.controller.browsing
                await pilot.press("ctrl+r", "g", "escape")
                assert not app.has_modal and not app.controller.browsing
                assert app.editor.text == ("draft stays here" if entry == "ctrl+r" else "")
                assert not session.main.agent.signal.is_set()
                release.set()
                await wait_for(pilot, lambda: app.controller.can_switch)
                if work == "agent":
                    assert has_reply(app, "finished")
                if work == "queued":
                    assert session.effort == "high"
                await pilot.pause()
                assert not app.has_modal
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("work", ["agent", "command", "helper", "queued", "new"])
def test_browser_keeps_running_work_safe_and_unlocks_after_completion(make_session, work):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def wait(*_args):
            entered.set()
            assert release.wait(15)
            return faux.reply("finished")

        ui = FullscreenUI()
        session = make_session(faux.reply("saved answer"), wait, ui=ui, persist=True)
        session.prompt("saved question")
        saved = session.store.path
        session.new_session()
        current = session.store.path
        session.commands.register("waiting", wait, concurrent=work == "helper")
        app = VirgoApp(session, ui, initial="running" if work == "agent" else "/waiting")
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await wait_for(pilot, entered.is_set)
                if work in {"queued", "new"}:
                    app.controller.submit("/new" if work == "new" else "/effort high")
                app.editor.load_text("draft stays here")
                await pilot.press("ctrl+r")
                picker = app.screen
                assert isinstance(picker, SessionPicker)
                choices = picker.query_one(OptionList)
                choices.highlighted = choices.get_option_index(str(current))
                await pilot.press("n", "ctrl+x")
                assert app.screen is picker and session.store.path == current
                await pilot.press("enter")
                assert not app.has_modal and app.editor.text == "draft stays here"
                assert not session.main.agent.signal.is_set()
                await pilot.press("ctrl+r")
                picker = app.screen
                choices = picker.query_one(OptionList)
                choices.highlighted = choices.get_option_index(str(saved))
                await pilot.press("enter")
                assert app.screen is picker and session.store.path == current
                assert picker.selected_path == saved
                assert app.editor.text == "draft stays here"
                assert choices.get_option(str(current)).prompt.status == "Running"
                release.set()
                await wait_for(pilot, lambda: picker.current_status == "Ready")
                assert app.controller.can_switch and app.controller.browsing
                if work == "queued":
                    assert session.effort == "high" and not app.controller.pending
                if work == "new":
                    assert picker.current_path == session.store.path != current
                    assert all(item["path"] != current for item in picker.sessions)
                if work == "agent":
                    assert has_reply(app, "finished")
                assert picker.selected_path == saved
                assert choices.get_option(str(session.store.path)).prompt.status == "Ready"
                await pilot.press("enter")
                await wait_for(pilot, lambda: session.store.path == saved and not app.controller.busy)
                assert has_reply(app, "saved answer")
                assert not app.has_modal and app.editor.text == ""
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "choice", "preferences"])
def test_browser_preserves_pending_input_and_focus(make_session, kind):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        answers = []

        def ask(*_args):
            answers.append(ui.select("Pick one", ["first", "second"]) if kind == "choice" else ui.input("Name"))

        session.commands.register("ask", ask)
        app = VirgoApp(session, ui, initial="/model" if kind == "preferences" else "/ask")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: app.active_input is not None)
            request_id = app.active_input
            if kind == "text":
                app.screen_stack[0].query_one("#auth-value", Input).value = "unfinished answer"
            elif kind == "choice":
                await pilot.press("down")
            focused = app.screen_stack[0].query_one({
                "text": "#auth-value", "choice": "#completions", "preferences": "#preference-models",
            }[kind])
            await wait_for(pilot, lambda: app.focused is focused)
            await pilot.press("ctrl+r")
            assert isinstance(app.screen, SessionPicker)
            assert app.screen.current_status == "Waiting"
            await pilot.press("n", "ctrl+x", "escape")
            assert not app.has_modal and not answers
            assert app.active_input == request_id and ui.is_input_pending(request_id)
            assert app.focused is focused and focused.display
            if kind == "text":
                assert focused.value == "unfinished answer"
            elif kind == "choice":
                assert focused.highlighted == 1
            await pilot.press("escape" if kind == "preferences" else "enter")
            await wait_for(pilot, lambda: app.active_input is None and not app.controller.busy)
            if kind != "preferences":
                assert answers == ["second" if kind == "choice" else "unfinished answer"]

    asyncio.run(scenario())


@pytest.fixture
def trashed_sessions(tmp_path, monkeypatch):
    from pathlib import Path

    trash = tmp_path / "trash"
    trash.mkdir()
    moved = []

    def move(path):
        source = Path(path)
        destination = trash / source.name
        source.rename(destination)
        moved.append(destination)

    monkeypatch.setattr("virgo.harness.store.send2trash", move)
    return moved


@pytest.mark.parametrize("saved", [False, True])
def test_ctrl_x_deletes_current_session_and_continues_in_new_session(make_session, trashed_sessions, saved):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("old answer"), faux.reply("new answer"), ui=ui, persist=True)
        if saved:
            session.prompt("old question")
        original = session.store
        path = original.path
        contents = path.read_bytes() if saved else None
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("draft stays here")
            await pilot.press("ctrl+r", "ctrl+x")
            await wait_for(pilot, lambda: session.store is not original)
            picker = app.screen
            assert isinstance(picker, SessionPicker)
            assert picker.sessions == []
            assert picker.query_one("#session-empty").display
            assert app.controller.browsing
            assert not session.main.messages
            assert session.store.path is not None and session.store.path != path
            assert original.path is None and not path.exists()
            assert len(trashed_sessions) == int(saved)
            if saved:
                assert trashed_sessions[0].read_bytes() == contents
            await pilot.press("ctrl+x", "escape")
            assert app.editor.text == "draft stays here"
            app.editor.load_text("new question")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy and len(session.main.messages) == 2)
            assert not path.exists()
            assert SessionStore.load(session.store.path).build_context()[0][0].text() == "new question"
            await pilot.press("ctrl+r")
            assert all(row["path"] != path for row in app.screen.sessions)
            await pilot.press("escape")
        assert not path.exists()

    asyncio.run(scenario())


def test_ctrl_x_failure_preserves_current_session_and_selection(make_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("saved answer"), ui=ui, persist=True)
        session.prompt("saved question")
        original = session.store
        contents = original.path.read_bytes()
        app = VirgoApp(session, ui)
        notices = []

        def fail(path):
            raise PermissionError("Trash unavailable")

        monkeypatch.setattr("virgo.harness.store.send2trash", fail)
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+r", "ctrl+x")
            assert session.store is original
            assert app.screen.selected_path == original.path
            assert len(app.screen.sessions) == 1
            assert original.path.read_bytes() == contents
            assert notices and "Cannot move session to Trash" in notices[0]
            await pilot.press("escape")
            assert has_reply(app, "saved answer")

    asyncio.run(scenario())


@pytest.mark.parametrize("startup", [False, True])
def test_ctrl_x_deletes_other_workspace_without_switching(make_session, tmp_path, trashed_sessions, startup):
    other = tmp_path / "other-workspace"
    other.mkdir()
    saved = make_session(faux.reply("other answer"), persist=True, cwd=other)
    saved.prompt("other question")
    path = saved.store.path
    saved.shutdown()

    async def scenario():
        from textual.widgets import OptionList

        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        original = session.store
        app = VirgoApp(session, ui, pick_session=startup)
        async with app.run_test(size=(80, 24)) as pilot:
            if not startup:
                await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            picker = app.screen
            choices = picker.query_one(OptionList)
            choices.highlighted = choices.get_option_index(str(path))
            await pilot.press("ctrl+x")
            assert app.screen is picker
            assert session.store is original and session.cwd != other
            assert not path.exists() and len(trashed_sessions) == 1
            assert all(row["path"] != path for row in picker.sessions)
            assert picker.query_one("#session-empty").display == startup
            await pilot.press("escape")

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
