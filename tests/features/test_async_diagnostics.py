"""Failures in background workers retain their original owner and cannot strand pending work."""

from __future__ import annotations

import json
import threading

import pytest

from virgo.agent.types import ToolContext, ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage
from virgo.features import background

from tests.support.harness import calls, tool_results, wait_until
from tests.support.shell import selected


def records(session):
    return [json.loads(line) for path in (session.paths.user / "logs").glob("*.jsonl")
            for line in path.read_text().splitlines()]


def test_background_failure_and_failed_notification_keep_original_session(make_session, monkeypatch):
    session = make_session()
    jobs, owner = session.services["jobs"], session.store
    entered, release = threading.Event(), threading.Event()

    def broken(signal):
        entered.set()
        assert release.wait(5)
        try:
            raise OSError("storage unavailable")
        except OSError as cause:
            raise RuntimeError("background operation failed") from cause

    def broken_notify(*args, **kwargs):
        raise RuntimeError("notification queue unavailable")

    monkeypatch.setattr(session, "notify", broken_notify)
    job = jobs.start_callable("private description", broken, agent="worker", tool_call_id="call-origin")
    try:
        assert entered.wait(5)
        session.new_session()
    finally:
        release.set()
    assert job.done.wait(5)
    assert job.status == "failed" and job.reported and not jobs.unreported()
    root = job.error_details
    assert root["phase"] == "background.callable"
    assert root["context"]["session_id"] == owner.id != session.store.id
    assert root["context"]["job_id"] == job.id and root["context"]["agent"] == "worker"
    assert root["context"]["tool_call_id"] == "call-origin"
    assert [node["exception_type"] for node in root["cause_chain"]] == ["RuntimeError", "OSError"]
    notification = next(item for item in job.related_errors if item["phase"] == "background.notify")
    assert notification["context"]["related_error_id"] == root["error_id"]
    assert notification["context"]["session_id"] == owner.id
    assert "private description" not in json.dumps(records(session))
    result = session.tool("job_output").execute({"id": job.id}, ToolContext("read-job", session.cwd, threading.Event()))
    assert result.details["error"]["error_id"] == root["error_id"]


class FakeProcess:
    returncode = 0

    def __init__(self, failure=None):
        self.failure = failure
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.failure == "poll" and self.polls == 1:
            raise OSError("poll unavailable")
        return self.returncode

    def wait(self, timeout=None):
        if self.failure == "wait":
            raise OSError("wait unavailable")
        return self.returncode


def fake_command_io(monkeypatch, project, *, failure=None, close_failure=False):
    path = project / "background-output.log"
    path.write_text("command output\n")

    class OutputFile:
        def close(self):
            if close_failure:
                raise OSError("close unavailable")

    monkeypatch.setattr(background, "open_output", lambda *args: (path, OutputFile()))
    monkeypatch.setattr(background, "spawn", lambda *args, **kwargs: FakeProcess(failure))
    return path


@pytest.mark.parametrize(("failure", "phase"), [
    ("poll", "background.watch"), ("wait", "background.watch"),
    ("close", "background.close"), ("read", "background.output"),
])
def test_command_watcher_failures_finish_and_report(make_session, monkeypatch, project, failure, phase):
    session = make_session()
    fake_command_io(monkeypatch, project, failure=failure, close_failure=failure == "close")
    if failure == "read":
        def broken_read(*args, **kwargs):
            raise OSError("read unavailable")

        monkeypatch.setattr(background, "read_output", broken_read)
    jobs = session.services["jobs"]
    job = jobs.start_command("private command", project)
    assert job.done.wait(5)
    assert job.reported and job.status == "failed" and not jobs.unreported()
    assert job.error_details["phase"] == phase
    assert failure + " unavailable" in job.error_details["message"]
    assert job.error_details["context"]["job_id"] == job.id
    assert job.error_details["traceback"]
    assert "private command" not in json.dumps(records(session))


def test_cleanup_failure_does_not_replace_watcher_failure(make_session, monkeypatch, project):
    session = make_session()
    fake_command_io(monkeypatch, project, failure="wait", close_failure=True)
    job = session.services["jobs"].start_command("private command", project)
    assert job.done.wait(5)
    assert job.error_details["message"] == "wait unavailable"
    assert any(error["phase"] == "background.close" and error["message"] == "close unavailable"
               for error in job.related_errors)


def test_start_cleanup_cannot_mask_spawn_error(make_session, monkeypatch, project):
    session = make_session()
    fake_command_io(monkeypatch, project, close_failure=True)

    def broken_spawn(*args, **kwargs):
        raise OSError("spawn root cause")

    monkeypatch.setattr(background, "spawn", broken_spawn)
    jobs = session.services["jobs"]
    with pytest.raises(OSError, match="spawn root cause") as caught:
        jobs.start_command("private command", project)
    assert not jobs.jobs and not jobs.unreported()
    assert caught.value._virgo_error_details["phase"] == "background.start"
    assert {record["message"] for record in records(session)} >= {"spawn root cause", "close unavailable"}


def test_cancelled_callable_is_killed_without_a_fatal_error(make_session):
    session = make_session()
    entered = threading.Event()

    def cancelled(signal):
        entered.set()
        assert signal.wait(5)
        raise RuntimeError("worker observed cancellation")

    jobs = session.services["jobs"]
    job = jobs.start_callable("private label", cancelled)
    assert entered.wait(5)
    jobs.kill(job)
    assert job.done.wait(5)
    assert job.status == "killed" and job.reported
    assert job.error_details["kind"] == "aborted"
    assert not any(record["kind"] == "fatal" for record in records(session))


def test_cancelled_callable_result_does_not_emit_error(make_session):
    session = make_session()
    job = session.services["jobs"].start_callable(
        "private label", lambda signal: ToolResult.error("cancelled", details={"status": "aborted"}),
    )
    assert job.done.wait(5)
    assert job.status == "killed" and job.error_details is None
    assert not records(session)


def test_custom_exception_metadata_cannot_strand_background_job(make_session):
    session = make_session()

    class RestrictedError(Exception):
        @property
        def _virgo_error_details(self):
            raise RuntimeError("metadata access forbidden")

    def broken(signal):
        raise RestrictedError("original failure")

    job = session.services["jobs"].start_callable("private label", broken)
    assert job.done.wait(5)
    assert job.status == "failed" and job.reported
    assert job.error_details["message"] == "original failure"


def test_shell_background_call_uses_bound_tool_source_after_session_switch(make_session, monkeypatch, project):
    session = make_session()
    original = session.main.review_source
    session.new_session()
    fake_command_io(monkeypatch, project, failure="wait")
    ctx = ToolContext("shell-origin", project, threading.Event(), "worker", review_source=original)
    session.tool(selected(session).tool_name).execute({"command": "private command", "background": True}, ctx)
    job = next(iter(session.services["jobs"].jobs.values()))
    assert job.done.wait(5)
    assert job.error_details["context"]["session_id"] == original.store.id != session.store.id
    assert job.error_details["context"]["agent"] == "worker"
    assert job.error_details["context"]["tool_call_id"] == "shell-origin"


def test_subagent_failure_reaches_parent_tool_with_same_error_id(make_session):
    session = make_session(
        faux.reply(tools=[("agent", {"description": "private label", "prompt": "private child prompt"})]),
        faux.error("child provider unavailable"), faux.reply("lead recovered"),
    )
    session.prompt("delegate")
    result = tool_results(session)[0]
    assert result.is_error and result.details["error"]["message"] == "child provider unavailable"
    error_id = result.details["error"]["error_id"]
    child_records = [row for row in records(session) if row["error_id"] == error_id]
    assert len(child_records) == 1 and child_records[0]["context"]["agent"].startswith("general#")
    assert session.store.custom("model_call")[0]["error_details"]["error_id"] == error_id
    assert "private label" not in json.dumps(child_records) and "private child prompt" not in json.dumps(child_records)


@pytest.mark.parametrize("before_tools", [False, True])
def test_subagent_cancellation_before_first_call_or_tools_is_not_a_fault(make_session, before_tools):
    session = make_session(faux.reply(tools=[("read", {"path": "must-not-be-read"})]))
    signal = threading.Event()
    if before_tools:
        def cancel_before_tools(event, runtime):
            if runtime.role == "subagent" and event.type == "message_end" and isinstance(event.message, AssistantMessage):
                runtime.agent.signal.set()

        session.bus.on("agent_event", cancel_before_tools)
    else:
        signal.set()
    result = session.services["subagents"].run("private prompt", "general", "private label", signal)
    assert result.is_error and result.as_text() == "Sub-agent was aborted."
    assert result.details["status"] == "aborted" and "error" not in result.details
    assert len(calls()) == int(before_tools)
    assert not records(session)


def test_subagent_cancellation_does_not_hide_an_actual_model_error(make_session):
    session = make_session(faux.error("actual provider failure"))
    signal = threading.Event()

    def cancel_after_response(event, runtime):
        if runtime.role == "subagent" and event.type == "message_end" and isinstance(event.message, AssistantMessage):
            runtime.agent.signal.set()

    session.bus.on("agent_event", cancel_after_response)
    result = session.services["subagents"].run("private prompt", "general", "private label", signal)
    assert result.is_error and "actual provider failure" in result.as_text()
    assert result.details["error"]["message"] == "actual provider failure"
    assert result.details.get("status") != "aborted"


def test_background_subagent_preserves_child_error_in_job(make_session):
    session = make_session(faux.error("background child unavailable"))
    ctx = ToolContext("agent-origin", session.cwd, threading.Event(), review_source=session.main.review_source)
    session.tool("agent").execute({"description": "private label", "prompt": "private prompt", "background": True}, ctx)
    job = next(iter(session.services["jobs"].jobs.values()))
    assert job.done.wait(5)
    assert job.status == "failed" and job.error_details["message"] == "background child unavailable"
    error_id = job.error_details["error_id"]
    assert session.store.custom("model_call")[0]["error_details"]["error_id"] == error_id
    assert len([row for row in records(session) if row["error_id"] == error_id]) == 1
    link = next(row for row in records(session) if row["code"] == "BACKGROUND_ERROR_LINK")
    assert link["context"]["related_error_id"] == error_id
    assert link["context"]["job_id"] == job.id and link["context"]["tool_call_id"] == "agent-origin"


def test_team_model_failure_and_notification_failure_keep_both_causes(make_session, monkeypatch):
    session = make_session(faux.error("teammate provider unavailable"))
    team = session.services["team"]

    def broken_notify(*args, **kwargs):
        raise RuntimeError("team queue unavailable")

    monkeypatch.setattr(session, "notify", broken_notify)
    member = team.spawn("alice", "private role", "private prompt", False, False)
    try:
        wait_until(lambda: member.state == "idle")
        original = member.runtime.messages[-1].error_details
        assert member.error_details["error_id"] == original["error_id"]
        assert any(error["phase"] == "team.notify" for error in member.related_errors)
        assert any(error["error_id"] == original["error_id"] for error in team.errors())
        raw = json.dumps(records(session))
        assert "private role" not in raw and "private prompt" not in raw
    finally:
        team.request_stop("alice", force=True)
        member.thread.join(5)
    assert not member.thread.is_alive() and member.state == "stopped"


def test_teammate_crash_after_switch_keeps_owner_and_finishes_cleanup(make_session, monkeypatch):
    session = make_session()
    team, original = session.services["team"], session.store
    entered, release = threading.Event(), threading.Event()
    create_runtime = session.create_runtime

    def create(name, role, **kwargs):
        runtime = create_runtime(name, role, **kwargs)
        if role == "teammate":
            def broken_prompt(*args):
                entered.set()
                assert release.wait(5)
                raise RuntimeError("teammate worker root cause")

            runtime.agent.prompt = broken_prompt
        return runtime

    def broken_release(*args):
        raise RuntimeError("task cleanup secondary failure")

    monkeypatch.setattr(session, "create_runtime", create)
    monkeypatch.setattr(session.services["tasks"], "release", broken_release)
    member = team.spawn("alice", "private role", "private prompt", True, False)
    try:
        assert entered.wait(5)
        session.new_session()
    finally:
        release.set()
        member.thread.join(5)
    assert not member.thread.is_alive() and member.state == "stopped"
    assert "alice" not in session.services["permissions"].agent_modes
    assert member.error_details["phase"] == "team.loop"
    assert member.error_details["context"]["accounting_session_id"] == original.id != session.store.id
    assert member.error_details["message"] == "teammate worker root cause"
    assert any(error["message"] == "task cleanup secondary failure" for error in member.related_errors)
