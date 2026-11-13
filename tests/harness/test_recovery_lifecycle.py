"""Evidence survives real session boundaries without driving extra model turns."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent
from virgo.diagnostics import capture_error
from virgo.harness.diagnostic_access import DiagnosticAccess
from virgo.harness.retry import retry_after_seconds

from tests.support.harness import calls, install_responses, tool_results
from tests.support.persistence import fail_session_write


def wire_text(request):
    return json.dumps([message["content"] for message in request["messages"]], ensure_ascii=False)


def failing_tool():
    def execute(*_args):
        try:
            raise OSError("fixture backend is unavailable")
        except OSError as cause:
            raise RuntimeError("fixture operation failed") from cause

    return AgentTool("fixture_failure", "fixture", {"type": "object"}, execute)


def install_failure(session):
    tool = failing_tool()
    session.register_tool(tool)
    session.main.tools.append(tool)


def test_error_references_survive_reloading_their_own_history(make_session):
    session = make_session(faux.reply(tools=[("fixture_failure", {})]), faux.reply("stopped"), persist=True,
                           refresh_models=False)
    install_failure(session)
    session.prompt("inspect")
    record = tool_results(session)[0].details["error"]
    path = session.store.path
    reloaded = make_session(session=path, persist=True, refresh_models=False)
    restored = reloaded.main.diagnostic_access.get(record["error_id"])
    assert restored["cause_chain"][-1]["message"] == "fixture backend is unavailable"
    stranger = make_session(refresh_models=False)
    assert stranger.main.diagnostic_access.get(record["error_id"]) is None


def test_compaction_retains_queryable_evidence_and_adds_one_checkpoint(make_session):
    session = make_session(faux.reply(tools=[("fixture_failure", {})]), faux.reply("stopped " * 100),
                           persist=True, refresh_models=False, settings={"compaction": {"keepRecentTokens": 1}})
    install_failure(session)
    session.prompt("original task")
    record = tool_results(session)[0].details["error"]
    faux.push("test", faux.reply("The user is inspecting a fixture."))
    assert session.compact() is not None
    faux.push("test", faux.reply("continued"), faux.reply("later"))
    session.prompt("continue")
    assert "Retained diagnostic references" in wire_text(calls()[-1])
    assert record["error_id"] in wire_text(calls()[-1])
    assert session.main.diagnostic_access.read(record["error_id"], "causes")["status"] == "ok"
    session.prompt("one more step")
    checkpoints = [message for message in session.main.messages
                   if getattr(message, "kind", None) == "notification"
                   and "Retained diagnostic references" in message.text()]
    assert len(checkpoints) == 1
    reloaded = make_session(session=session.store.path, persist=True, refresh_models=False)
    assert reloaded.main.diagnostic_access.get(record["error_id"]) is not None
    faux.push("test", faux.reply("after reload"))
    reloaded.prompt("continue after reload")
    assert wire_text(calls()[-1]).count("Retained diagnostic references") == 1


def test_background_failure_targets_original_runtime_after_switch(make_session):
    session = make_session(faux.reply("initial"), persist=True, refresh_models=False)
    session.prompt("original task")
    original, entered, release = session.main, threading.Event(), threading.Event()

    def work(_signal):
        entered.set()
        assert release.wait(5)
        raise OSError("background owner evidence")

    job = session.services["jobs"].start_callable("fixture", work)
    try:
        assert entered.wait(5)
        session.new_session()
    finally:
        release.set()
    assert job.done.wait(5)
    assert not session.wakeups
    assert session.main.diagnostic_access.get(job.error_details["error_id"]) is None
    assert original.diagnostic_access.get(job.error_details["error_id"]) is not None
    session.open_session(original.store.path)
    faux.push("test", faux.reply("checked failure"))
    assert session.run_pending()
    assert job.error_details["error_id"] in wire_text(calls()[-1])
    assert "background owner evidence" in wire_text(calls()[-1])


def test_notification_failure_retains_evidence_without_waking_another_runtime(make_session, monkeypatch):
    session = make_session(faux.reply("initial"), persist=True, refresh_models=False)
    session.prompt("original task")
    original, entered, release = session.main, threading.Event(), threading.Event()

    def work(_signal):
        entered.set()
        assert release.wait(5)
        raise OSError("delivery-independent evidence")

    def broken_notify(*_args, **_kwargs):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(session, "notify", broken_notify)
    job = session.services["jobs"].start_callable("fixture", work)
    try:
        assert entered.wait(5)
        session.new_session()
    finally:
        release.set()
    assert job.done.wait(5)
    assert not session.wakeups
    session.open_session(original.store.path)
    faux.push("test", faux.reply("inspected"))
    session.prompt("continue")
    assert "delivery-independent evidence" in wire_text(calls()[-1])


def test_expired_pending_references_cannot_block_retained_evidence(make_session):
    session = make_session(faux.reply("inspected"), refresh_models=False)
    recovery = session.main.recovery
    recovery.access = session.main.diagnostic_access = DiagnosticAccess(max_records=2, max_ids=8)
    records = [capture_error(f"cause {index}", phase="fixture.pending") for index in range(5)]
    for record in records:
        recovery.queue(record)
    session.prompt("inspect pending facts")
    body = wire_text(calls()[-1])
    assert "no longer retained" in body
    assert records[-1]["error_id"] in body and "cause 4" in body
    assert records[-2]["error_id"] in body and "cause 3" in body


def test_reported_fault_does_not_start_an_extra_model_turn(make_session):
    session = make_session(faux.reply("answer"), refresh_models=False)
    session.prompt("task")
    session.ui.woken.clear()
    record = session.main.report_error(ValueError("late observation"), phase="fixture.late")
    assert len(calls()) == 1 and not session.ui.woken.is_set()
    assert not session.run_pending() and len(calls()) == 1
    faux.push("test", faux.reply("next answer"))
    session.prompt("next request")
    assert record["error_id"] in wire_text(calls()[-1])
    assert len(calls()) == 2


def test_model_diagnostics_feature_can_be_disabled_without_disabling_logs(make_session):
    session = make_session(faux.reply(tools=[("fixture_failure", {})]), faux.reply("stopped"),
                           refresh_models=False, settings={"features": {"diagnostics": False}})
    install_failure(session)
    session.prompt("inspect")
    assert session.tool("diagnostic_get") is None
    assert "Diagnostic evidence" not in wire_text(calls()[-1])
    assert session.diagnostics.recent()


def test_mcp_refresh_preserves_runtime_local_tools(make_session):
    session = make_session(faux.reply(tools=[("runtime_only", {})]), faux.reply("done"), refresh_models=False)
    tool = AgentTool("runtime_only", "local", {"type": "object"}, lambda *_args: "local result")
    child = session.create_runtime("worker", "subagent", tools=[tool])
    session.services["mcp_refresh"] = lambda _runtime: True
    child.agent.prompt("use your local tool")
    assert child.tools == [tool]
    assert "local result" in wire_text(calls()[-1])


def test_session_lifecycle_handler_failure_reaches_next_request(make_session):
    session = make_session(faux.reply("inspected"), refresh_models=False)

    def failed(_session):
        raise OSError("session lifecycle evidence")

    session.bus.on("session_start", failed)
    session.new_session()
    record = next(item for item in session.diagnostics.recent() if item["message"] == "session lifecycle evidence")
    session.prompt("inspect")
    assert record["error_id"] in wire_text(calls()[-1])
    assert session.main.diagnostic_access.get(record["error_id"]) is not None


def test_diagnostic_delivery_save_failure_stops_before_request_hooks(make_session, monkeypatch):
    session = make_session(faux.reply("must not run"), persist=True, refresh_models=False)
    session.main.report_error(OSError("queued evidence"), phase="fixture.observation")
    contexts = []
    session.bus.on("context", lambda view, runtime: contexts.append(runtime) or view)
    with fail_session_write(monkeypatch, session.store.path, containing="Runtime failure observations"):
        session.prompt("continue")
    assert session.store.save_error is not None
    assert not contexts and not calls()


@pytest.mark.parametrize("value, expected", [("2", 2), ("0", 0), ("-1", None), ("nan", None),
                                            ("inf", None), ("bad", None), (None, None), (True, None)])
def test_retry_after_values(value, expected):
    assert retry_after_seconds({"context": {"http": {"retry_after": value}}}) == expected


def test_retry_after_http_date():
    date = datetime(2026, 11, 14, 1, 2, tzinfo=UTC)
    details = {"context": {"http": {"retry_after": format_datetime(date, usegmt=True)}}}
    assert retry_after_seconds(details, now=date.timestamp() - 7) == 7
    assert retry_after_seconds(details, now=date.timestamp() + 7) == 0


@pytest.mark.parametrize("budget, requests, sleeps", [(10, 2, [4.0]), (2, 1, [])])
def test_provider_retry_after_uses_existing_attempts_and_wait_budget(make_session, monkeypatch,
                                                                   budget, requests, sleeps):
    failure = AssistantMessage(stop_reason="error", error_message="rate limited", error_kind="retryable")
    failure.error_details = capture_error("rate limited", phase="provider.stream", kind="retryable",
                                         context={"http": {"status_code": 429, "retry_after": "4"}})
    session = make_session(refresh_models=False, settings={"retry": {"maxWaitSeconds": budget}})
    seen = install_responses(monkeypatch, failure, AssistantMessage([TextContent("recovered")]))
    waited = []
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda delay: waited.append(delay) or False)
    session.prompt("task")
    assert waited == sleeps and len(seen) == requests
    assert not session.run_pending() and len(seen) == requests
    if requests == 2:
        assert session.main.agent.last_text == "recovered"
