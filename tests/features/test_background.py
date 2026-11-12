"""Background jobs notify the session and never stay pending after they end."""

from __future__ import annotations

import threading

import pytest

from virgo.ai.providers import faux
from virgo.features import background

from tests.support.harness import RecordingUI, calls, last_message_text, tool_results, wait_until
from tests.support.shell import selected


def test_background_job_notifies_idle_session(make_session, python_command):
    ui = RecordingUI()
    s = make_session(ui=ui)
    shell = selected(s)
    command = python_command("import time; time.sleep(0.3); print('built')", shell=shell)
    faux.push(
        "test",
        faux.reply(tools=[(shell.tool_name, {"command": command, "background": True})]),
        faux.reply("started"),
    )
    s.prompt("build in background")
    assert "Started background job job1" in tool_results(s)[0].text()
    assert ui.woken.wait(5)
    faux.push("test", faux.reply("saw the result"))
    assert s.run_pending()
    note = last_message_text(calls()[-1])
    assert "job1" in note and "completed" in note and "built" in note


def pending_work(session) -> bool:
    return any(session.bus.emit("pending_work", session))


def test_job_that_cannot_start_is_not_left_pending(make_session, project, monkeypatch):
    session = make_session()
    jobs = session.services["jobs"]

    def broken(*args, **kwargs):
        raise OSError("no shell")

    monkeypatch.setattr(background, "spawn", broken)
    with pytest.raises(OSError):
        jobs.start_command("echo hi", project)
    assert not jobs.jobs and not pending_work(session)
    assert not list((project / ".virgo" / "outputs").glob("*.log"))


def test_finished_job_is_pending_until_its_notification_is_queued(make_session, project, monkeypatch):
    session = make_session()
    jobs = session.services["jobs"]
    entered, release = threading.Event(), threading.Event()
    notify = session.notify

    def slow_notify(*args, **kwargs):
        entered.set()
        release.wait(5)
        notify(*args, **kwargs)

    monkeypatch.setattr(session, "notify", slow_notify)
    job = jobs.start_command("true", project)
    assert entered.wait(5)
    assert job.status == "completed" and pending_work(session)  # print mode must keep waiting
    release.set()
    wait_until(lambda: not pending_work(session))
    assert session.wakeups


def test_failed_job_notification_is_reported_and_does_not_hold_print_mode(make_session, project, monkeypatch):
    session = make_session()
    jobs = session.services["jobs"]

    def broken_notify(*args, **kwargs):
        raise RuntimeError("queue gone")

    monkeypatch.setattr(session, "notify", broken_notify)
    job = jobs.start_command("true", project)
    wait_until(lambda: job.reported)
    assert not pending_work(session)
    assert f"could not deliver the notification for {job.id}" in session.ui.text
    assert "queue gone" in session.ui.text
