"""Errors remain diagnosable after retries, session switches and failed disk writes."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from virgo.agent import AgentTool, ToolResult
from virgo.ai import catalog
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.ai.validate import INVALID_ARGUMENTS
from virgo.diagnostics import capture_error, error_summary, safe_text, sanitize
from virgo.execution import ExecutionFailed, ExecutionUnavailable
from virgo.harness.diagnostics import DiagnosticLog
from virgo.harness.store import SessionStore

from tests.support.harness import tool_results
from tests.support.persistence import diagnostic_records, fail_session_write
from tests.support.sandbox import python_command
from tests.support.shell import selected


def records(session):
    return diagnostic_records(session.diagnostics.path)


def chained_error():
    try:
        raise ValueError("inner failure")
    except ValueError as cause:
        try:
            raise RuntimeError("outer failure") from cause
        except RuntimeError as error:
            return error


def test_capture_preserves_chain_and_locations_without_locals_or_source():
    private_local = "a private local variable that must not be captured"
    details = capture_error(chained_error(), phase="tool.execute", context={"tool_call_id": "call-7"})
    assert details["code"] == "TOOL_EXECUTE_FAILED"
    assert details["cause_chain"][0]["exception_type"] == "RuntimeError"
    assert details["cause_chain"][1]["exception_type"] == "ValueError"
    assert details["cause_chain"][1]["relation"] == "cause"
    assert "chained_error" in details["traceback"]
    assert "inner failure" in details["traceback"] and "outer failure" in details["traceback"]
    assert details["context"]["tool_call_id"] == "call-7"
    assert private_local not in json.dumps(details)
    assert 'raise ValueError(' not in details["traceback"]
    assert "RuntimeError: outer failure" in error_summary(details)


def test_error_summary_shows_message_before_reference_and_redacts_all_fields(monkeypatch):
    secret = "private-placeholder"
    monkeypatch.setenv("VIRGO_TEST_TOKEN", secret)
    error_id = "1" * 32
    details = {
        "code": f"CODE_{secret}", "error_id": error_id, "phase": f"tool.{secret}",
        "exception_type": f"Failure_{secret}", "message": f"Lost connection {secret}",
        "context": {"agent": "main", "model": "model-" * 20 + secret, "tool_name": "bash"},
        "log_path": f"/logs/{secret}",
    }
    summary = error_summary(details)
    assert "Lost connection" in summary[:100]
    assert summary.index("Lost connection") < summary.index(error_id)
    assert all(field in summary for field in (
        "tool.[redacted]", "main / " + "model-" * 20 + "[redacted] / bash", "Failure_[redacted]",
        "Lost connection [redacted]", "CODE_[redacted]", "/logs/[redacted]",
    ))
    assert secret not in summary


def test_routine_shell_and_file_errors_only_surface_as_tool_results(make_session, monkeypatch, project):
    session = make_session(persist=True, refresh_models=False)
    notices = []
    monkeypatch.setattr(session.ui, "print", lambda text, style=None: notices.append((text, style)))
    shell = selected(session).tool_name
    (project / "example.txt").write_text("original")
    faux.script("test", faux.reply(tools=[
        (shell, {"command": "exit 52"}),
        (shell, {"command": python_command(session, "import time; time.sleep(5)"), "timeout": 0.1}),
        ("read", {"path": "missing.txt"}),
        ("edit", {"path": "example.txt", "edits": [{"old_text": "absent", "new_text": "change"}]}),
    ]), faux.reply("done"))
    session.prompt("Run the checks")

    command, timeout, missing, mismatch = tool_results(session)
    assert command.details["exit_code"] == 52
    assert command.details["boundary"]["payload_started"] is True
    assert timeout.details["status"] == "timeout"
    assert "missing.txt" in missing.text()
    assert "example.txt" in mismatch.text() and (project / "example.txt").read_text() == "original"
    ends = [ev for ev in session.ui.events if ev.type == "tool_execution_end"]
    assert len(ends) == 4
    for message in (command, timeout, missing, mismatch):
        event = next(ev for ev in ends if ev.tool_call_id == message.tool_call_id)
        assert event.is_error and message.is_error
        assert message.content == event.result.content
        assert "[Diagnostic evidence;" not in message.text()
        assert message.details["execution"]["outcome"] == "returned_error"
        assert "error" not in message.details
    assert not records(session)
    assert not SessionStore.load(session.store.path).custom("runtime_diagnostic")
    assert not [text for text, style in notices if style == "error"]


@pytest.mark.parametrize(("name", "args", "stop_reason"), [
    ("missing-tool", {}, "tool_use"), ("read", {INVALID_ARGUMENTS: "null"}, "tool_use"),
    ("read", {}, "tool_use"), ("read", {"path": "partial"}, "length"),
])
def test_invalid_calls_return_an_inline_error_without_crash_reporting(make_session, name, args, stop_reason):
    session = make_session(faux.reply(tools=[(name, args)], stop_reason=stop_reason), faux.reply("done"),
                           persist=True, refresh_models=False)
    session.prompt("inspect")
    result, = tool_results(session)
    assert result.is_error and result.text()
    assert result.details["execution"]["started"] is False
    assert not records(session) and not session.store.custom("runtime_diagnostic")


@pytest.mark.parametrize(("fault", "status", "started", "effects"), [
    (ExecutionUnavailable, "execution_unavailable", False, "none"),
    (ExecutionFailed, "execution_failed", "unknown", "possible"),
])
def test_shell_fault_evidence_is_queryable_without_replay(broker_session, monkeypatch, fault, status, started, effects):
    from virgo.sandbox.native import NativeExecutor

    launches = []

    def fail(*args, **kwargs):
        launches.append(1)
        raise fault("backend fault")

    def lookup(context):
        summary = json.loads(context.messages[-1].content[-1].text.splitlines()[1])
        return faux.reply(tools=[("diagnostic_get", {"error_id": summary["error_id"]})])

    session = broker_session(persist=True, refresh_models=False, approval="never")
    monkeypatch.setattr(NativeExecutor, "run_process", fail)
    faux.script("test", faux.reply(tools=[(selected(session).tool_name, {"command": "echo test"})]),
                lookup, faux.reply("done"))
    session.prompt("run")
    result, evidence = tool_results(session)
    assert len(launches) == 1 and result.details["status"] == status
    assert result.text().count("[Diagnostic evidence;") == 2
    boundary = json.loads(result.content[-2].text.splitlines()[1])["boundary"]
    assert boundary["payload_started"] == started and boundary["side_effects"] == effects
    error = result.details["error"]
    assert error["kind"] == "fatal" and error["error_id"] in session.ui.text
    assert records(session)[0]["error_id"] == error["error_id"]
    page = json.loads(evidence.text())
    assert page["status"] == "ok" and page["error_id"] == error["error_id"]


@pytest.mark.parametrize(("status", "cleanup", "kind"), [
    ("ok", "failed", "fatal"), ("resource_denied", None, "blocked"), ("high_risk_denied", None, "blocked"),
])
def test_returned_execution_faults_and_denials_keep_scoped_evidence(make_session, status, cleanup, kind):
    session = make_session(faux.reply(tools=[("probe", {})]), faux.reply("done"), persist=True, refresh_models=False)
    boundary = ({"payload_started": True, "side_effects": "possible", "cleanup": cleanup} if cleanup else
                {"payload_started": False, "side_effects": "none", "kind": status})
    tool = AgentTool("probe", "", {"type": "object"}, lambda *_: ToolResult.error(
        "Aborted" if cleanup else "Denied", details={"status": status, "boundary": boundary}))
    session.register_tool(tool)
    session.prompt("probe")
    result, = tool_results(session)
    assert result.details["boundary"] == boundary
    assert json.loads(result.content[-2].text.splitlines()[1])["boundary"] == boundary
    error = result.details["error"]
    assert error["kind"] == kind
    assert (error["error_id"] in session.ui.text) == (kind == "fatal")
    assert records(session)[0]["error_id"] == error["error_id"]
    assert session.main.diagnostic_access.read(error["error_id"], "summary")["status"] == "ok"


def test_redaction_covers_credentials_payloads_urls_and_exception_override(monkeypatch):
    monkeypatch.setenv("ACME_API_KEY", "environment-credential")
    message = (
        'environment-credential Authorization: Bearer unlisted-token '
        'password="hidden password" https://user:pass@api.example/v1?key=private-query#fragment explicit-value'
    )
    error = RuntimeError(message)
    details = capture_error(error, phase="provider.stream", secrets=("explicit-value",), context={
        "api_key": "private-key", "headers": {"Authorization": "private-auth"},
        "prompt": "private prompt", "args": {"text": "private args"}, "http": {"status_code": 429},
    })
    encoded = json.dumps(details)
    for secret in ("environment-credential", "unlisted-token", "hidden password", "private-query",
                   "explicit-value", "private-key", "private-auth", "private prompt", "private args", "user:pass"):
        assert secret not in encoded
    assert "api.example/v1" in encoded and details["context"]["http"]["status_code"] == 429
    safe = capture_error(RuntimeError("entire SDK response body"), phase="provider.stream", message="rate limited")
    assert "entire SDK response body" not in json.dumps(safe)
    assert "rate limited" in safe["traceback"]


def test_diagnostics_are_bounded_and_json_serializable_with_broken_exception_text():
    class BrokenError(Exception):
        def __str__(self):
            raise RuntimeError("bad formatter")

    value = {"nested": [], "large": "x" * 40_000, "nan": float("nan"), "opaque": object()}
    value["nested"].append(value)
    safe = sanitize(value)
    assert safe["nested"] == ["[cycle omitted]"]
    assert "truncated" in safe["large"]
    assert safe["opaque"] == "<object>"
    json.dumps(safe, allow_nan=False)
    details = capture_error(BrokenError(), phase="test")
    assert "text unavailable" in details["message"]
    assert "\x1b" not in safe_text("\x1b[31merror\x00")


def test_diagnostic_log_parallel_writes_are_complete_private_and_deduplicated(tmp_path):
    log = DiagnosticLog(tmp_path / "diagnostics")
    errors = [capture_error(f"failure {index}", phase="test") for index in range(30)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(log.record, errors + errors))
    saved = diagnostic_records(log.path)
    assert len(saved) == 30
    assert {record["error_id"] for record in saved} == {error["error_id"] for error in errors}
    assert all(record["log_path"] == str(log.path) for record in saved)
    if os.name != "nt":
        assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(log.path.parent.stat().st_mode) == 0o700


def test_diagnostic_log_history_is_complete_and_only_touches_its_own_files(tmp_path):
    log = DiagnosticLog(tmp_path)
    unrelated = tmp_path / "another.jsonl"
    unrelated.write_text("keep")
    for number in range(120):
        log.record(capture_error(f"error {number} " + "x" * 200, phase="test"))
    assert unrelated.read_text() == "keep"
    saved = diagnostic_records(log.path)
    assert len(saved) == 120
    assert saved[0]["message"].startswith("error 0")
    assert saved[-1]["message"].startswith("error 119")


def test_log_write_failure_keeps_original_diagnostic_on_stderr(tmp_path, monkeypatch, capsys):
    log = DiagnosticLog(tmp_path / "logs")
    error = capture_error(chained_error(), phase="tool.execute")

    def fail(*args, **kwargs):
        raise PermissionError("diagnostic directory is read-only")

    monkeypatch.setattr("virgo.harness.diagnostic_store.os.open", fail)
    saved = log.record(error)
    assert "log_path" not in saved
    assert "read-only" in saved["logging_error"]
    fallback = capsys.readouterr().err
    assert error["error_id"] in fallback and "inner failure" in fallback and "outer failure" in fallback
    assert log.record(error) is saved


def test_retry_failure_survives_success_and_reload_without_prompt_contents(make_session, monkeypatch):
    session = make_session(faux.error("temporary overload", kind="retryable"), faux.reply("private answer"), persist=True)
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)
    session.prompt("private user prompt")
    calls = SessionStore.load(session.store.path).custom("model_call")
    assert len(calls) == 1 and calls[0]["purpose"] == "retry"
    assert calls[0]["error_kind"] == "retryable"
    assert calls[0]["error_message"] == "temporary overload"
    failure = calls[0]["error_details"]
    assert calls[0]["attempt"] == 1 and calls[0]["max_retries"] == 3
    assert failure["context"]["run_id"] and failure["context"]["model_call_id"]
    assert failure["context"]["session_id"] == session.store.id
    assert records(session)[0]["error_id"] == failure["error_id"]
    assert "private user prompt" not in json.dumps(records(session))
    assert "private answer" not in json.dumps(records(session))
    assert session.main.messages[-1].text() == "private answer"


def test_final_model_error_is_written_once_and_exposed_with_its_log_path(make_session):
    session = make_session(faux.error("provider unavailable"), persist=True)
    session.prompt("work")
    failure = session.main.messages[-1].error_details
    assert len(records(session)) == 1
    assert failure["error_id"] in session.ui.text
    assert str(session.diagnostics.path) in session.ui.text
    restored = SessionStore.load(session.store.path).build_context()[0][-1]
    assert restored.error_details == failure


def test_auxiliary_and_child_errors_keep_original_owner_after_session_switch(make_session):
    session = make_session(persist=True)
    original = session.store
    child = session.create_runtime("worker", "subagent", tools=[])
    session.new_session()
    faux.script("test", faux.error("auxiliary failed"), faux.error("child failed"))
    result = session.complete("review", "private review system", "private review prompt",
                              accounting_store=original, tool_call_id="review-call")
    child.agent.prompt("private child prompt")
    assert result.error_details["context"]["session_id"] == original.id
    assert child.messages[-1].error_details["context"]["session_id"] == original.id
    assert child.messages[-1].error_details["context"]["agent"] == "worker"
    assert len(original.custom("model_call")) == 2
    assert not session.store.custom("model_call")
    assert len(records(session)) == 2
    raw = json.dumps(records(session))
    assert "private review" not in raw and "private child prompt" not in raw


def test_tool_failure_and_recovered_result_hook_keep_original_evidence(make_session):
    session = make_session(faux.reply(tools=[("broken", {})]), faux.reply("done"), persist=True)

    def broken(args, context):
        raise chained_error()

    session.register_tool(AgentTool("broken", "failure", {"type": "object"}, broken))
    session.main.tools = session.tools_for("main")
    session.main.agent.cfg.tools = session.main.tools
    session.bus.on("tool_result", lambda result, *_: ToolResult.text("recovered"))
    session.prompt("work")
    tool = next(message for message in session.main.messages if isinstance(message, ToolResultMessage))
    assert not tool.is_error and tool.text() == "recovered"
    assert tool.details["related_errors"][0]["exception_type"] == "RuntimeError"
    failure = records(session)[0]
    assert failure["context"]["tool_name"] == "broken"
    assert failure["context"]["tool_call_id"] == tool.tool_call_id
    assert "inner failure" in failure["traceback"]


def test_policy_exception_stays_fail_closed_and_preserves_diagnostic_id(make_session, project, monkeypatch):
    session = make_session(faux.reply(tools=[("write", {"path": "blocked.txt", "content": "private"})]),
                           faux.reply("done"), persist=True)

    class BrokenPolicy:
        def preflight(self, ev):
            return None

        def before_execute(self, ev):
            raise chained_error()

    monkeypatch.setattr(session.services["execution"], "before_execute", BrokenPolicy().before_execute)
    session.prompt("write")
    assert not (project / "blocked.txt").exists()
    result = next(message for message in session.main.messages if isinstance(message, ToolResultMessage))
    failure = result.details["error"]
    assert failure["phase"] == "permission.before_execute" and result.is_error
    saved = records(session)
    assert len(saved) == 1 and saved[0]["error_id"] == failure["error_id"]
    assert "inner failure" in saved[0]["traceback"]


def test_session_save_failure_uses_independent_log_and_keeps_unsaved_history(make_session, monkeypatch):
    session = make_session(faux.reply("must not run"), persist=True)
    with fail_session_write(monkeypatch, session.store.path):
        session.prompt("accepted prompt")
    failure = records(session)[0]
    assert failure["phase"] == "session.save"
    assert failure["context"]["session_file"] == str(session.store.path)
    assert failure["cause_chain"][1]["exception_type"] == "OSError"
    assert "unsaved" in session.ui.text and failure["error_id"] in session.ui.text
    assert any(message.text() == "accepted prompt" for message in session.main.messages)
    count = len(records(session))
    session.prompt("queued prompt")
    assert len(records(session)) == count


def test_failing_ui_does_not_destroy_original_error_or_recurse(make_session, monkeypatch, capsys):
    session = make_session()

    def fail(*args, **kwargs):
        raise RuntimeError("UI failed")

    monkeypatch.setattr(session.ui, "print", fail)
    error = chained_error()
    original = session.report_error(error, phase="session.run")
    duplicate = session.report_error(error, phase="cli.run")
    assert duplicate["error_id"] == original["error_id"]
    assert len(records(session)) == 1
    assert "inner failure" in records(session)[0]["traceback"]
    assert "UI failed" in capsys.readouterr().err


def test_catalog_worker_retains_cause_behind_safe_public_message(make_session, monkeypatch):
    session = make_session()

    def fail(*args, **kwargs):
        raise RuntimeError("transport initialization failed")

    monkeypatch.setattr(catalog, "_fetch", fail)
    with pytest.raises(catalog.CatalogError, match="Could not read the model directory") as captured:
        catalog.fetch_model_catalog(session.model)
    details = session.report_error(captured.value, phase="catalog.refresh")
    assert details["cause_chain"][1]["exception_type"] == "RuntimeError"
    assert "transport initialization failed" in details["traceback"]


def test_system_section_failure_is_recorded_without_breaking_construction(make_session):
    session = make_session()

    def fail(role):
        raise chained_error()

    session.sections.append((50, "broken-section", fail))
    session.main.rebuild_system()
    failure = records(session)[0]
    assert failure["phase"] == "extension.system_section"
    assert failure["context"]["section"] == "broken-section"
    assert "inner failure" in failure["traceback"]


def test_diagnostics_command_inspects_records_without_changing_model_history(make_session):
    session = make_session()
    session.main.messages.append(UserMessage("existing context"))
    error = session.report_error(chained_error(), phase="test")
    session.prompt("/diagnostics")
    assert error["error_id"] in session.ui.text
    session.prompt(f"/diagnostics {error['error_id'][:12]}")
    assert "cause_chain" in session.ui.text
    session.prompt("/diagnostics unknown")
    assert "unknown or ambiguous" in session.ui.text
    assert len(session.main.messages) == 1


@pytest.mark.parametrize("kind", ["stop", "aborted"])
def test_success_and_explicit_cancel_do_not_create_error_log(make_session, kind):
    session = make_session(faux.reply("finished", stop_reason=kind))
    session.prompt("work")
    assert not session.diagnostics.path.exists()
