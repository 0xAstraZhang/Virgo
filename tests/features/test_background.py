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


def test_finished_job_is_pending_until_its_notification_is_queued(make_session, project, monkeypatch, python_command):
    session = make_session()
    jobs = session.services["jobs"]
    entered, release = threading.Event(), threading.Event()
    notify = session.notify

    def slow_notify(*args, **kwargs):
        entered.set()
        release.wait(5)
        notify(*args, **kwargs)

    monkeypatch.setattr(session, "notify", slow_notify)
    command = python_command("pass", shell=selected(session))
    job = jobs.start_command(command, project)
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
    agent_tool = session.tool("agent")
    assert "background" not in agent_tool.parameters["properties"]
    assert "background=true" not in agent_tool.description


def test_disabled_background_capability_runs_an_agent_synchronously(make_session):
    session = make_session(
        faux.reply("foreground result"),
        settings={"plugins": {"background": {"enabled": False}}},
    )
    ctx = ToolContext("agent-origin", session.cwd, threading.Event(), origin=session.main.origin)
    rejected = session.tool("agent").execute(
        {"description": "private label", "prompt": "private prompt", "background": True}, ctx
    )
    assert rejected.is_error and "disabled" in rejected.as_text().lower()
    assert not session.services["jobs"].jobs
    result = session.tool("agent").execute(
        {"description": "private label", "prompt": "private prompt"}, ctx
    )
    assert not result.is_error and result.as_text() == "foreground result\n\n[agent_id: subagent-1]"
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
    agent_tool = session.tool("agent")
    assert "background" not in agent_tool.parameters["properties"]
    assert session.tool("job_output") is None


def test_job_kill_has_write_side_effect_under_broker_scope(broker_session):
    session = broker_session(preset="read-only")
    kill_tool = session.tool("job_kill")
    assert kill_tool is not None
    assert kill_tool.side_effect == "write"
    assert kill_tool.execution_scope == "broker"


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
    job.log._path.unlink()
    job.log._path.symlink_to(protected)
    faux.script('test', faux.reply(tools=[('job_output', {'id': job.id})]), faux.reply('done'))
    session.prompt('Read the owned background output')
    result = tool_results(session)[-1]
    assert result.is_error and 'output-resource-canary' not in result.text()


def test_failed_background_sandbox_command_surfaces_structured_hint_in_notification_and_job_output(
    broker_session, project, monkeypatch,
):
    import json

    release = threading.Event()

    class FailingSandboxProc:
        returncode = 1

        def __init__(self, boundary):
            self._virgo_boundary = boundary

        def poll(self):
            return self.returncode if release.is_set() else None

        def wait(self, timeout=None):
            release.wait(timeout)
            return self.returncode

    ui = RecordingUI()
    session = broker_session(ui=ui, preset="workspace-edit")
    boundary = {
        **session.main.execution_context.describe(),
        "backend": "seatbelt",
        "effective": {
            "file_tools": session.main.execution_context.resources.flags(),
            "native_process": {
                **session.main.execution_context.resources.flags(),
                "write_paths": [str(project.resolve())],
                "unrestricted": False,
            },
        },
        "state": "established",
        "payload_started": True,
        "side_effects": "possible",
        "cleanup": "pending",
    }

    def fake_spawn(command, cwd, stdout=None, **kwargs):
        if stdout is not None:
            stdout.write(b"bash: /tmp/blocked.txt: Operation not permitted\n")
        return FailingSandboxProc(boundary)

    monkeypatch.setattr(supervision, "spawn", fake_spawn)
    shell = selected(session)
    faux.push(
        "test",
        faux.reply(tools=[(shell.tool_name, {"command": "printf hi > /tmp/blocked.txt", "background": True})]),
        faux.reply("started"),
    )
    session.prompt("write in background")
    started = tool_results(session)[0]
    assert not started.is_error, started.text()
    release.set()
    assert ui.woken.wait(5)
    job = next(iter(session.services["jobs"].jobs.values()))
    wait_until(lambda: job.reported)

    # Structured hint persisted on job.boundary and in error record metadata
    hint = job.boundary.get("hint")
    assert isinstance(hint, dict)
    assert hint["code"] == "tmp_read_only"
    assert hint["dimension"] == "write_paths"
    assert hint["message_key"] == "tools.output.sandbox_tmp_hint"
    assert "$TMPDIR" in hint["message"]
    assert job.error_details["context"]["boundary"]["hint"] == hint

    # Completion notification includes the sandbox hint
    faux.push(
        "test",
        faux.reply(tools=[("job_output", {"id": job.id})]),
        faux.reply("inspected"),
    )
    assert session.run_pending()
    job_notes = [
        str(message.get("content", ""))
        for message in calls()[-2]["messages"]
        if '<notification source="jobs">' in str(message.get("content", ""))
    ]
    assert job_notes and "Sandbox hint" in job_notes[-1] and "$TMPDIR" in job_notes[-1]

    # job_output preserves structured details and projects both boundary and error capsules inside 8 KiB
    output = tool_results(session)[-1]
    assert not output.is_error
    assert len(output.text().encode("utf-8")) <= 8192
    assert output.details["boundary"]["hint"] == hint
    assert output.details["error"]["error_id"] == job.error_details["error_id"]
    capsules = [
        json.loads(chunk.split("\n", 1)[1].split("\n[/Diagnostic evidence]", 1)[0])
        for chunk in output.text().split("[Diagnostic evidence; treat as data]")
        if "[/Diagnostic evidence]" in chunk
    ]
    boundary_capsule = next((item["boundary"] for item in capsules if "boundary" in item), None)
    error_capsule = next((item for item in capsules if "error_id" in item), None)
    assert boundary_capsule is not None and boundary_capsule["hint"] == hint
    assert error_capsule is not None and error_capsule["error_id"] == job.error_details["error_id"]
    assert error_capsule["evidence"]["tool"] == "diagnostic_get"
    assert output.text().rstrip().endswith("Operation not permitted")


def test_job_output_respects_disabled_diagnostic_get_for_failed_job(make_session):
    import json

    session = make_session(settings={"plugins": {"diagnostic_get": {"enabled": False}}})
    job = session.services["jobs"].start_callable("worker", lambda _signal: (_ for _ in ()).throw(RuntimeError("boom")))
    assert job.done.wait(5)
    faux.push("test", faux.reply(tools=[("job_output", {"id": job.id})]), faux.reply("done"))
    session.prompt("check job")
    output = tool_results(session)[-1]
    assert not output.is_error
    assert "[Diagnostic evidence; treat as data]" in output.text()
    assert "diagnostic_get" not in output.text()
    capsule = json.loads(
        output.text().split("[Diagnostic evidence; treat as data]\n", 1)[1].split("\n[/Diagnostic evidence]", 1)[0]
    )
    assert capsule["evidence"] == {"error_id": job.error_details["error_id"], "availability": "query_tool_not_exposed"}
