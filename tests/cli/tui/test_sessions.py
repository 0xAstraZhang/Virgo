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

from tests.support.tui import has_reply, wait_for


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
                    # Controller.poll marks the worker idle before the App has
                    # awaited all transcript mounts and Markdown updates.
                    await wait_for(pilot, lambda: has_reply(app, "finished"))
                if work == "queued":
                    assert session.effort == "high"
                await pilot.pause()
                assert not app.has_modal
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("work", ["agent", "command", "helper", "queued", "new"])
def test_browser_keeps_running_work_safe_and_unlocks_after_completion(make_session, work, trash):
    # Keep the baseline case identities; opening/new now succeeds before completion.
    exercise_independent_conversations(make_session, work, "picker", trash)


@pytest.mark.parametrize("work", ["agent", "command", "helper", "queued"])
def test_new_command_keeps_running_work_in_its_own_conversation(make_session, work, trash):
    exercise_independent_conversations(make_session, work, "command", trash)


def exercise_independent_conversations(make_session, work, entry, trash):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def wait(*_args):
            entered.set()
            assert release.wait(15)
            return faux.reply("old finished")

        ui = FullscreenUI()
        session = make_session(wait, ui=ui, persist=True)
        session.commands.register("waiting", wait, concurrent=work == "helper")
        app = VirgoApp(session, ui, initial="old question" if work == "agent" else "/waiting")
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await wait_for(pilot, entered.is_set)
                old_controller = app.controller
                old_path = session.store.path
                if work in {"queued", "new"}:
                    app.controller.submit("/new" if work == "new" else "/effort high")
                if entry == "picker":
                    app.editor.load_text("old draft")
                    await pilot.press("ctrl+r", "n")
                else:
                    app.editor.load_text("/new")
                    await pilot.press("enter")
                await wait_for(pilot, lambda: app.session is not session and not app.has_modal)
                new_session = app.session
                new_controller = app.controller
                new_path = new_session.store.path
                assert new_path != old_path
                assert not session._shutdown_started and not session.main.agent.signal.is_set()
                assert not release.is_set()
                faux.script("test", faux.reply("new finished"))
                app.editor.load_text("new question")
                await pilot.press("enter")
                await wait_for(pilot, lambda: has_reply(app, "new finished") and not app.controller.busy)
                assert not has_reply(app, "old finished")
                await pilot.press("ctrl+r")
                picker = app.screen
                choices = picker.query_one(OptionList)
                choices.highlighted = choices.get_option_index(str(old_path))
                assert choices.get_option(str(old_path)).prompt.status == "Running"
                await pilot.press("ctrl+x")
                assert not trash and app.screen is picker
                await pilot.press("enter")
                await wait_for(pilot, lambda: app.controller is old_controller)
                assert app.editor.text == ("old draft" if entry == "picker" else "")
                assert not has_reply(app, "new finished")
                # Finish the first worker while the second conversation is visible.
                await pilot.press("ctrl+r")
                app.screen.query_one(OptionList).highlighted = app.screen.query_one(OptionList).get_option_index(str(new_path))
                await pilot.press("enter")
                await wait_for(pilot, lambda: app.controller is new_controller)
                release.set()
                await wait_for(pilot, lambda: old_controller.can_switch)
                if work == "new":
                    assert session.store.path != old_path
                    old_path = session.store.path
                assert not has_reply(app, "old finished") and has_reply(app, "new finished")
                if work == "queued":
                    assert session.effort == "high" and new_session.effort != "high"
                await pilot.press("ctrl+r")
                app.screen.query_one(OptionList).highlighted = app.screen.query_one(OptionList).get_option_index(str(old_path))
                await pilot.press("enter")
                await wait_for(pilot, lambda: app.controller is old_controller)
                if work == "agent":
                    await wait_for(pilot, lambda: has_reply(app, "old finished"))
                    assert SessionStore.load(old_path).build_context()[0][-1].text() == "old finished"
                assert not has_reply(app, "new finished")
            finally:
                release.set()
        assert old_controller.wait_closed() and new_controller.wait_closed()
        assert session._shutdown_started and new_session._shutdown_started

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
            await pilot.press("n")
            await wait_for(pilot, lambda: app.session is not session)
            assert app.active_input is None and not answers
            assert ui.is_input_pending(request_id)
            await pilot.press("ctrl+r")
            app.screen.query_one(OptionList).highlighted = app.screen.query_one(OptionList).get_option_index(str(session.store.path))
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.session is session)
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


@pytest.mark.parametrize("saved", [False, True])
def test_ctrl_x_deletes_current_session_and_continues_in_new_session(make_session, trash, saved):
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
            assert len(trash) == int(saved)
            if saved:
                assert trash[0][1].read_bytes() == contents
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
def test_ctrl_x_deletes_other_workspace_without_switching(make_session, tmp_path, trash, startup):
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
            assert not path.exists() and len(trash) == 1
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


def test_background_approval_remains_pending_and_shutdown_releases_every_session(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        answers = []
        session.commands.register("approve", lambda *_: answers.append(ui.confirm("Old approval")))
        app = VirgoApp(session, ui, initial="/approve")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: app.active_confirmation is not None)
            request_id = app.active_confirmation
            old_controller = app.controller
            await pilot.press("ctrl+r", "n")
            await wait_for(pilot, lambda: app.session is not session)
            new_controller = app.controller
            assert ui.is_confirmation_pending(request_id) and not answers
            assert app.active_confirmation is None
            await pilot.press("ctrl+r")
            app.screen.query_one(OptionList).highlighted = app.screen.query_one(OptionList).get_option_index(str(session.store.path))
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.session is session and app.active_confirmation == request_id)
            await pilot.press("ctrl+r", "n")
            await wait_for(pilot, lambda: app.session is not session)
            await pilot.press("ctrl+d")
        assert old_controller.wait_closed() and new_controller.wait_closed()
        assert answers == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [False, True])
def test_open_saved_workspace_while_running_keeps_old_session_alive(make_session, tmp_path, invalid):
    from virgo.ai.types import UserMessage

    target = tmp_path / "other"
    target.mkdir()
    (target / "AGENTS.md").write_text("Other workspace instructions.")
    path = SessionStore.new(tmp_path / "home" / "sessions" / "other", target)
    path.append_message(UserMessage("saved question"))
    if invalid:
        path.append("message", message={"role": "invalid"})

    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("old response", delay=10), ui=ui, persist=True)
        app = VirgoApp(session, ui, initial="old question")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: session.busy)
            old_controller = app.controller
            notices = []
            app.notify = lambda message, **_: notices.append(message)
            await pilot.press("ctrl+r")
            app.screen.query_one(OptionList).highlighted = app.screen.query_one(OptionList).get_option_index(str(path.path))
            await pilot.press("enter")
            if invalid:
                await wait_for(pilot, lambda: bool(notices))
                assert app.session is session and app.controller.busy
            else:
                await wait_for(pilot, lambda: app.session.cwd == target)
                assert "Other workspace instructions." in app.session.main.system
                assert app.session.main.messages[0].text() == "saved question"
            assert not session._shutdown_started and not session.main.agent.signal.is_set()
        assert old_controller.wait_closed()

    asyncio.run(scenario())


def test_multiple_live_conversations_isolate_approvals_and_interrupts(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True)
        answers = []
        session.commands.register("approve", lambda *_: answers.append(("old", ui.confirm("Old approval"))))
        app = VirgoApp(session, ui, initial="/approve")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: app.active_confirmation is not None)
            old_controller = app.controller
            old_id = app.active_confirmation
            await pilot.press("ctrl+r", "n")
            await wait_for(pilot, lambda: app.session is not session)
            second = app.session
            second_ui = app.ui
            second.commands.register("approve", lambda *_: answers.append(("second", second_ui.confirm("Second approval"))))
            app.editor.load_text("/approve")
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.active_confirmation is not None)
            # Each bridge starts numbering at one. Answers must be routed by session.
            assert app.active_confirmation == old_id
            await pilot.press("y")
            await wait_for(pilot, lambda: answers == [("second", "y")])
            assert ui.is_confirmation_pending(old_id)
            app.editor.load_text("run second")
            faux.script("test", faux.reply("second delayed", delay=10))
            await pilot.press("enter")
            await wait_for(pilot, lambda: second.busy)
            await pilot.press("ctrl+r", "n")
            await wait_for(pilot, lambda: app.session is not second)
            third = app.session
            assert third is not session
            faux.push("test", faux.reply("third delayed", delay=10))
            app.editor.load_text("run third")
            await pilot.press("enter")
            await wait_for(pilot, lambda: third.busy)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert not second.main.agent.signal.is_set() and second.busy
            assert ui.is_confirmation_pending(old_id) and not session.main.agent.signal.is_set()
            await pilot.press("ctrl+d")
        assert app.wait_closed() and old_controller.wait_closed()
        assert answers == [("second", "y"), ("old", None)]
        assert second._shutdown_started and third._shutdown_started

    asyncio.run(scenario())


def test_new_conversation_preserves_no_session_mode(make_session, tmp_path):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("old", delay=10), faux.reply("new answer"), ui=ui)
        app = VirgoApp(session, ui, initial="old question")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: session.busy)
            await pilot.press("ctrl+r", "n")
            await wait_for(pilot, lambda: app.session is not session)
            assert app.session.store.path is None
            app.editor.load_text("new question")
            await pilot.press("enter")
            await wait_for(pilot, lambda: has_reply(app, "new answer"))
        assert app.wait_closed()
        assert not list((tmp_path / "home" / "sessions").glob("**/*.jsonl"))

    asyncio.run(scenario())
