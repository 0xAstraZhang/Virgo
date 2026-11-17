"""Background jobs notify the session and never stay pending after they end."""

from __future__ import annotations

import threading

import pytest

from virgo.agent.types import ToolContext
from virgo.ai.providers import faux
from virgo.harness import jobs as supervision

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

    monkeypatch.setattr(supervision, "spawn", broken)
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

def test_disabled_background_capability_keeps_the_shell_foreground_only(make_session):
    session = make_session(settings={"plugins": {"background": {"enabled": False}}})
    jobs = session.services["jobs"]
    assert not jobs.background_enabled
    assert session.tool("job_output") is None and session.tool("job_kill") is None
    assert session.commands.get("jobs") is None
    shell_tool = session.tool(selected(session).tool_name)
    assert "background" not in shell_tool.parameters["properties"]


def test_disabled_background_capability_runs_an_agent_synchronously(make_session):
    session = make_session(
        faux.reply("foreground result"),
        settings={"plugins": {"background": {"enabled": False}}},
    )
    ctx = ToolContext("agent-origin", session.cwd, threading.Event(), origin=session.main.origin)
    result = session.tool("agent").execute(
        {"description": "private label", "prompt": "private prompt", "background": True}, ctx
    )
    assert not result.is_error and result.as_text() == "foreground result"
    assert not session.services["jobs"].jobs

def test_failed_background_setup_keeps_the_shell_foreground_only(make_session, monkeypatch):
    from virgo.plugins import background

    def broken(api):
        raise ValueError("background setup failed")

    monkeypatch.setattr(background, "setup", broken)
    session = make_session()
    assert "background" not in session.loaded_plugins
    assert not session.services["jobs"].background_enabled
    shell_tool = session.tool(selected(session).tool_name)
    assert "background" not in shell_tool.parameters["properties"]
    assert session.tool("job_output") is None


def test_job_tools_cannot_borrow_another_conversations_resources(make_session, project):
    session = make_session()
    owner = session.main
    jobs = session.services['jobs']
    release = threading.Event()
    job = jobs.start_callable('private work', lambda _signal: release.wait(5) and 'private-job-result',
                              store=owner.origin.root, cwd=project)
    try:
        session.new_session()
        context = ToolContext('foreign-job', project, threading.Event(), origin=session.main.origin)
        output = session.tool('job_output').execute({'id': job.id}, context)
        assert output.is_error and 'private work' not in output.as_text()
        stopped = session.tool('job_kill').execute({'id': job.id}, context)
        assert stopped.is_error and not job.signal.is_set()
        release.set()
        assert job.done.wait(5)
        context = ToolContext('owned-job', project, threading.Event(), origin=owner.origin)
        output = session.tool('job_output').execute({'id': job.id}, context)
        assert not output.is_error and 'private-job-result' in output.as_text()
    finally:
        release.set()
        job.done.wait(5)


def test_background_output_alias_cannot_read_outside_resource_boundary(broker_session, project):
    session = broker_session()
    jobs = session.services['jobs']
    job = jobs.start_callable('owned output', lambda _signal: 'done', store=session.main.origin.root, cwd=project)
    assert job.done.wait(5)
    protected = project.parent/'output-secret.txt'
    protected.write_text('output-resource-canary')
    job.log, stream = session.main.execution_context.open_output(project, 'owned-output')
    stream.close()
    job.log.unlink()
    job.log.symlink_to(protected)
    faux.script('test', faux.reply(tools=[('job_output', {'id': job.id})]), faux.reply('done'))
    session.prompt('Read the owned background output')
    result = tool_results(session)[-1]
    assert result.is_error and 'output-resource-canary' not in result.text()
