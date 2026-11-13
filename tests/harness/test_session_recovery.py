"""Recovery through real prompts, background accounting and session commands.

Failures: a late background save loses its owning runtime after switching;
partial or missing JSONL hides in-memory history from /sessions; reopening
restarts models/writes or loses pending input; cached main and child models
disagree; trash leaves a cached path that can resurrect the conversation.
"""

from __future__ import annotations

import threading

import pytest

from virgo.ai.providers import faux
from virgo.harness.store import SessionStore

from tests.support.harness import calls, wait_until
from tests.support.persistence import fail_session_write


def test_late_background_save_failure_cannot_be_bypassed(make_session, monkeypatch):
    s = make_session(faux.reply("original answer"), faux.reply("healthy answer"), persist=True)
    s.prompt("original question")
    original = s.main
    path = s.store.path
    entered, release = threading.Event(), threading.Event()

    def delayed(*_args):
        entered.set()
        assert release.wait(5)
        return faux.reply("background result")

    faux.script("helper", delayed)
    job = s.services["jobs"].start_callable(
        "background accounting",
        lambda signal: s.complete("background", "", "work", model=s.resolve("faux/helper"),
                                  signal=signal, accounting_store=original.store).text(),
    )
    try:
        assert entered.wait(5)
        assert not s.wakeups and original.store.save_error is None
        s.prompt("/new")
        healthy = s.main
        with fail_session_write(monkeypatch, path):
            release.set()
            wait_until(lambda: job.reported)
        saved = path.read_bytes()
        assert original.store.save_error is not None
        s.prompt("healthy question")
        assert healthy.agent.last_text == "healthy answer"
        s.open_session(path)
        assert s.main is original
        assert s.store.custom("model_call")[-1]["purpose"] == "background"
        count = len(calls())
        s.prompt("unprocessed input")
        s.open_session(healthy.store.path)
        s.open_session(path)
        assert s.wakeups[-1].text() == "unprocessed input"
        notices = [message for message in s.wakeups if message.kind == "notification"]
        assert len(notices) == 1 and "Background job" in notices[0].text()
        assert not healthy.wakeups  # The late result belongs to the failed original task.
        assert not s.run_pending() and len(calls()) == count
        assert path.read_bytes() == saved
    finally:
        release.set()


@pytest.mark.parametrize("partial", [False, True])
def test_sessions_command_reopens_unsaved_history(make_session, monkeypatch, partial):
    s = make_session(faux.reply("unsaved answer"), persist=True)
    original, path = s.main, s.store.path
    with fail_session_write(monkeypatch, path, partial=partial,
                            containing="unsaved answer" if partial else ""):
        s.prompt("unsaved question")
    saved = path.read_bytes()
    s.prompt("pending input")
    pending = list(s.wakeups)
    s.prompt("/new")
    assert not SessionStore.list(s.paths.sessions)
    s.ui.lines.clear()
    s.prompt("/sessions")
    assert path.name in s.ui.text
    s.prompt("/sessions 1")
    assert s.main is original and list(s.wakeups) == pending
    if partial:
        assert any(message.text() == "unsaved answer" for message in s.main.messages)
    count = len(calls())
    s.prompt("must stay paused")
    assert len(calls()) == count and path.read_bytes() == saved


def test_restored_runtime_uses_selected_model_for_wakeups_and_children(make_session):
    s = make_session(faux.reply("M1 answer"), persist=True)
    s.prompt("original question")
    original, path = s.main, s.store.path
    s.notify("queued for M2")
    s.prompt("/new")
    s.prompt("/model faux/changed")
    faux.script("changed", faux.reply(tools=[("agent", {"prompt": "child", "description": "child"})]),
                faux.reply("M2 child"), faux.reply("M2 final"))
    s.open_session(path)
    s.prompt("/model")
    assert "faux/changed" in s.ui.text
    assert s.main is original and s.run_pending()
    assert s.main.agent.last_text == "M2 final"
    assert len(calls()) == 1 and len(calls("changed")) == 3


def test_trash_removes_retained_session_from_recovery(make_session, monkeypatch, trash):
    s = make_session(faux.reply("saved answer"), persist=True)
    s.prompt("saved question")
    path = s.store.path
    with fail_session_write(monkeypatch, path, partial=True):
        s.prompt("unsaved question")
    s.prompt("/new")
    s.trash_session(path)
    s.ui.lines.clear()
    s.prompt("/sessions")
    assert path.name not in s.ui.text
    with pytest.raises(FileNotFoundError):
        s.open_session(path)
