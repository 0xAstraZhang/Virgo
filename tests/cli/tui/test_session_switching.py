"""Session browsing must preserve workspace boundaries and shutdown ownership."""

from __future__ import annotations

import json
import threading

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import UserMessage
from virgo.cli.tui import controller as controller_module
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.controller import RunController
from virgo.harness.settings import Paths
from virgo.harness.store import SessionStore

from tests.support.harness import wait_until
from tests.support.tui import pump


def saved_session(cwd):
    cwd.mkdir(exist_ok=True)
    store = SessionStore.new(Paths(cwd).sessions, cwd)
    store.append_message(UserMessage("saved question"))
    return store.path


def test_switch_workspace_reloads_configuration_tools_and_services(make_session, tmp_path):
    target = tmp_path / "another-project"
    path = saved_session(target)
    (target / "AGENTS.md").write_text("Use the other workspace's instructions.")
    (target / ".virgo").mkdir()
    (target / ".virgo" / "settings.json").write_text(json.dumps({"effort": "high"}))
    ui = FullscreenUI()
    previous = make_session(faux.reply("continued"), ui=ui, persist=True, extensions=False)
    previous.permission_mode = "plan"
    previous.services["permissions"].session_rules.append("bash(*)")
    controller = RunController(previous, ui)
    controller.start()
    assert controller.open_session(path, initial="continue here")
    updates = pump(controller)
    current = controller.session
    try:
        assert current is not previous and previous._shutdown_started
        assert current.cwd == target and current.main.cwd == target
        assert current.paths.project == target / ".virgo"
        assert current.effort == "high"
        assert "other workspace's instructions" in current.main.system
        assert current.permission_mode == "accept-all"
        assert not current.services["permissions"].session_rules
        assert [m.text() for m in current.main.messages if isinstance(m, UserMessage)] == [
            "saved question", "continue here"
        ]
        assert current.main.agent.last_text == "continued"
        changed = next(i for i, event in enumerate(updates) if event.kind == "session_changed")
        assert updates[changed].payload is current
        assert updates[changed + 1].kind == "reset"
        assert ui._signals["main"] is current.main.agent.signal
    finally:
        controller.request_close()
        assert controller.wait_closed()


def test_same_workspace_open_preserves_session_and_resets_history(make_session, project):
    path = saved_session(project)
    ui = FullscreenUI()
    session = make_session(ui=ui)
    controller = RunController(session, ui)
    controller.start()
    assert controller.open_session(path)
    updates = pump(controller)
    assert controller.session is session
    assert not session._shutdown_started
    assert session.main.messages[0].text() == "saved question"
    assert any(event.kind == "reset" for event in updates)


@pytest.mark.parametrize("failure", ["missing-file", "missing-workspace", "invalid-settings"])
def test_invalid_target_retains_previous_session_and_does_not_send_initial(make_session, tmp_path, failure):
    target = tmp_path / "target"
    path = saved_session(target)
    if failure == "missing-file":
        path.unlink()
    elif failure == "missing-workspace":
        target.rmdir()
    else:
        (target / ".virgo").mkdir()
        (target / ".virgo" / "settings.json").write_text("{")
    ui = FullscreenUI()
    previous = make_session(ui=ui)
    controller = RunController(previous, ui)
    controller.start()
    assert controller.open_session(path, initial="must not run")
    updates = pump(controller)
    assert controller.session is previous and not previous._shutdown_started
    assert not faux.CALLS["test"]
    assert any(event.kind == "print" and event.payload[1] == "error" for event in updates)


@pytest.mark.parametrize("pause_stage", ["construction", "old-shutdown"])
def test_close_during_workspace_switch_stops_the_candidate(make_session, tmp_path, monkeypatch, pause_stage):
    path = saved_session(tmp_path / "target")
    ui = FullscreenUI()
    previous = make_session(ui=ui)
    controller = RunController(previous, ui)
    controller.start()
    entered, release = threading.Event(), threading.Event()
    created = []
    real_session = controller_module.AgentSession

    def construct(*args, **kwargs):
        candidate = real_session(*args, **kwargs)
        created.append(candidate)
        if pause_stage == "construction":
            entered.set()
            assert release.wait(3)
        return candidate

    def block_shutdown(_session):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(controller_module, "AgentSession", construct)
    if pause_stage == "old-shutdown":
        previous.bus.on("session_shutdown", block_shutdown)
    try:
        assert controller.open_session(path, initial="must not run")
        assert entered.wait(3)
        controller.request_close()
        release.set()
        assert controller.wait_closed()
        assert created and created[0]._shutdown_started
        assert controller.session is previous
        assert not faux.CALLS["test"]
        assert not any(event.kind == "session_changed" for event in controller.poll())
    finally:
        release.set()
        for candidate in created:
            candidate.shutdown()


def test_browsing_pauses_wakeups_but_drains_deferred_commands(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("notification handled"), ui=ui)
    controller = RunController(session, ui)
    controller.start()
    controller.browsing = True
    session.notify("pending notification")
    controller._deferred.append("/effort high")
    pump(controller, lambda c: not c.busy and not c.pending)
    assert controller.can_switch and session.wakeups and session.effort == "high"
    assert not faux.CALLS["test"]
    controller.browsing = False
    pump(controller, lambda c: not c.busy and not session.wakeups and not c.pending)
    assert session.effort == "high"
    assert session.main.agent.last_text == "notification handled"


def test_open_and_new_are_rejected_while_helper_is_running(make_session, project):
    path = saved_session(project)
    ui = FullscreenUI()
    session = make_session(ui=ui)
    entered, release = threading.Event(), threading.Event()
    session.commands.register("waiting", lambda *_: (entered.set(), release.wait(3)), concurrent=True)
    controller = RunController(session, ui)
    controller.start()
    try:
        controller.submit("/waiting")
        assert entered.wait(3)
        assert not controller.can_switch
        assert not controller.open_session(path)
        assert not controller.new_session()
    finally:
        release.set()
    pump(controller, lambda c: c.can_switch)
    assert controller.new_session()
    pump(controller)
    assert not session.main.messages
    controller.request_close()
    assert controller.wait_closed()
    wait_until(lambda: controller.closing)
    assert not controller.open_session(path)


def test_queued_commands_finish_before_session_can_be_opened(make_session, project):
    path = saved_session(project)
    ui = FullscreenUI()
    session = make_session(ui=ui)
    controller = RunController(session, ui)
    controller.start()
    controller._deferred.append("/effort high")
    assert not controller.can_switch
    assert not controller.open_session(path)
    assert not controller.new_session()
    pump(controller, lambda c: c.can_switch)
    assert session.effort == "high"
    assert controller.open_session(path)
    pump(controller)
    assert session.store.path == path


def test_close_removes_empty_record_written_after_shutdown_hooks(make_session, monkeypatch):
    ui = FullscreenUI()
    session = make_session(ui=ui, persist=True)
    session.set_model("faux/before")
    path = session.store.path
    assert path.exists()
    entered, first_cleanup = threading.Event(), threading.Event()
    cleanup = session.cleanup_empty_stores

    def observe_cleanup():
        cleanup()
        first_cleanup.set()

    def late_model_change(_session, _args):
        entered.set()
        assert first_cleanup.wait(3)
        session.set_model("faux/after")
        assert path.exists()

    monkeypatch.setattr(session, "cleanup_empty_stores", observe_cleanup)
    session.commands.register("late-model", late_model_change)
    controller = RunController(session, ui)
    controller.start()
    controller.submit("/late-model")
    assert entered.wait(3)
    controller.request_close()
    assert controller.wait_closed()
    assert not path.exists()


def test_old_teammate_finishes_persisting_without_updating_new_workspace(make_session, tmp_path):
    path = saved_session(tmp_path / "target")
    entered, release = threading.Event(), threading.Event()

    def blocked_reply(_context):
        entered.set()
        assert release.wait(3)
        return faux.reply("old workspace teammate response")

    ui = FullscreenUI()
    previous = make_session(blocked_reply, ui=ui, persist=True)
    controller = RunController(previous, ui)
    controller.start()
    member = previous.services["team"].spawn("reviewer", "review", "old task", False, False)
    try:
        assert entered.wait(3)
        assert controller.open_session(path)
        pump(controller)
        assert controller.session is not previous
        before_count = len(member.runtime.store.entries)
        release.set()
        member.thread.join(3)
        assert not member.thread.is_alive()
        late = controller.poll()
        assert not [update for update in late if update.kind == "event"]
        assert len(member.runtime.store.entries) > before_count
        saved = SessionStore.load(member.runtime.store.path)
        assert saved.entries[-1]["message"]["role"] == "assistant"
        assert ui._signals["main"] is controller.session.main.agent.signal
    finally:
        release.set()
        member.thread.join(3)
        controller.request_close()
        assert controller.wait_closed()
