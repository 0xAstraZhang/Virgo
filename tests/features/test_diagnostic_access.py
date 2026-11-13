"""The model can inspect explicitly shared evidence without crossing runtime scopes."""

from __future__ import annotations

import json
import threading

from virgo.agent.types import ToolContext
from virgo.diagnostics import capture_error
from virgo.features.diagnostics import setup
from virgo.harness.diagnostic_access import MAX_RECORD_BYTES, DiagnosticAccess
from virgo.harness.extensions import ExtensionAPI


def record(error_id, message="backend unavailable", **context):
    result = capture_error(message, phase="mcp.read", context=context)
    result["error_id"] = error_id
    return result


def collection(runtime):
    if not isinstance(getattr(runtime, "diagnostic_access", None), DiagnosticAccess):
        runtime.diagnostic_access = DiagnosticAccess()
    return runtime.diagnostic_access


def diagnostic_tool(session):
    if session.tool("diagnostic_get") is None:
        setup(ExtensionAPI(session, "diagnostics"))
    return session.tool("diagnostic_get")


def query(tool, runtime, error_id, **args):
    context = ToolContext("diagnostic-call", runtime.cwd, threading.Event(), agent=runtime.name,
                          review_source=runtime.review_source)
    result = tool.execute({"error_id": error_id, **args}, context)
    assert not result.is_error  # A normal query miss must not manufacture another diagnostic.
    return json.loads(result.as_text())


def test_shared_accounting_store_does_not_grant_child_or_sibling_evidence(make_session):
    session = make_session(refresh_models=False)
    tool = diagnostic_tool(session)
    child = session.create_runtime("worker", "subagent", tools=[])
    sibling = session.create_runtime("other-worker", "subagent", tools=[])
    private = record("child-error", "child-only-evidence", session_id=session.store.id, agent=child.name)
    collection(child).remember(private)
    collection(session.main)
    collection(sibling)

    assert query(tool, child, "child-error")["status"] == "ok"
    assert query(tool, session.main, "child-error")["status"] == "not_found"
    assert query(tool, sibling, "child-error")["status"] == "not_found"
    collection(session.main).remember(private)  # Explicit propagation, not accounting ownership.
    assert "child-only-evidence" in query(tool, session.main, "child-error")["text"]
    assert query(tool, sibling, "child-error")["status"] == "not_found"


def test_query_uses_bound_runtime_after_ui_session_switch(make_session):
    session = make_session(refresh_models=False)
    tool = diagnostic_tool(session)
    original = session.main
    collection(original).remember(record("original-error", "original-evidence"))
    session.new_session()
    collection(session.main).remember(record("new-error", "new-evidence"))

    assert "original-evidence" in query(tool, original, "original-error")["text"]
    assert query(tool, original, "new-error")["status"] == "not_found"
    assert query(tool, session.main, "original-error")["status"] == "not_found"
    assert "new-evidence" in query(tool, session.main, "new-error")["text"]


def test_tool_rejects_missing_or_foreign_runtime_without_looking_at_active_session(make_session, project):
    session = make_session(refresh_models=False)
    tool = diagnostic_tool(session)
    collection(session.main).remember(record("known"))
    no_source = tool.execute({"error_id": "known"}, ToolContext("call", project, threading.Event()))
    assert json.loads(no_source.as_text())["status"] == "denied"

    foreign = make_session(refresh_models=False)
    collection(foreign.main).remember(record("known", "foreign-private-evidence"))
    result = query(tool, foreign.main, "known")
    assert result["status"] == "denied"
    assert "foreign-private-evidence" not in json.dumps(result)


def test_allowlist_redacts_evidence_and_preserves_execution_constraints(monkeypatch):
    monkeypatch.setenv("BACKEND_API_KEY", "a-known-private-key-12345")
    access = DiagnosticAccess()
    source = record(
        "safe-error", "failed with a-known-private-key-12345",
        runtime_id="worker-1", server="backend", generation=4, candidate_generation=5, dispatch="sent",
        stderr_tail="Authorization: Bearer private-auth-98765\nModuleNotFoundError: adapter",
        arbitrary_private_object={"password": "never-send", "business_record": "customer-private-data"},
        arguments={"private": "tool-input"},
        http={"status_code": 429, "endpoint": "https://user:pass@example.test/api?key=private-query",
              "headers": {"private": "request-header"}},
        execution={"started": True, "outcome": "unknown", "side_effects": "unknown", "private": "execution-secret"},
        transport={"server": "backend", "generation": 4, "dispatch": "sent", "private": "transport-secret"},
    )
    source["unrecognized_business_data"] = "unrecognized-private-data"
    source["log_path"] = "/private/diagnostics-secret-file.jsonl"
    access.remember(source)
    retained = access.get("safe-error")
    data = json.dumps(retained)
    for private in ("a-known-private-key-12345", "private-auth-98765", "customer-private-data", "tool-input",
                    "private-query", "request-header", "execution-secret", "transport-secret",
                    "unrecognized-private-data", "diagnostics-secret-file"):
        assert private not in data
    assert "[redacted]" in data
    assert retained["context"]["execution"] == {"started": True, "outcome": "unknown", "side_effects": "unknown"}
    assert retained["context"]["transport"]["dispatch"] == "sent"
    assert retained["context"]["generation"] == 4
    assert retained["context"]["candidate_generation"] == 5
    assert "ModuleNotFoundError" in access.read("safe-error", "stderr")["text"]
    assert "stderr_tail" not in json.loads(access.read("safe-error")["text"])["context"]


def test_pages_respect_encoded_utf8_budget_and_reassemble_evidence():
    access = DiagnosticAccess(page_bytes=1024)
    source = record("unicode-error")
    source["traceback"] = '根因🙂 "quoted" \\path\n' * 1800
    access.remember(source)
    retained = access.get("unicode-error")
    assert len(json.dumps(retained, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_RECORD_BYTES

    pieces, cursor = [], None
    for _ in range(200):
        page = access.read("unicode-error", "trace", cursor)
        assert page["status"] == "ok"
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
        assert page["text"]
        pieces.append(page["text"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and len(pieces) > 1
    assert "".join(pieces) == retained["traceback"]


def test_cursor_cannot_cross_error_section_or_evidence_snapshot():
    access = DiagnosticAccess(page_bytes=512)
    source = record("first")
    source["traceback"] = "source trace " * 200
    access.remember(source)
    access.remember({**source, "error_id": "second"})
    cursor = access.read("first", "trace")["next_cursor"]
    assert cursor is not None
    assert access.read("second", "trace", cursor)["status"] == "invalid_cursor"
    assert access.read("first", "summary", cursor)["status"] == "invalid_cursor"
    access.remember({**source, "traceback": "changed trace " * 200})
    assert access.read("first", "trace", cursor)["status"] == "invalid_cursor"


def test_expired_unknown_and_uncaptured_evidence_are_distinct_without_path_fallback(tmp_path):
    path = tmp_path / "private.jsonl"
    path.write_text("private-file-payload")
    access = DiagnosticAccess(max_records=1, max_ids=3)
    access.remember({**record("older-error"), "log_path": str(path), "log_pattern": str(tmp_path / "*")})
    access.remember(record("newer-error"))

    assert access.read("older-error")["status"] == "expired"
    assert access.get("older-error") is None
    for unknown in ("newer", str(path), "../private.jsonl", "another-runtime-error"):
        assert access.read(unknown)["status"] == "not_found"
    for section in ("causes", "trace", "stderr", "attempts"):
        assert access.read("newer-error", section)["status"] == "unavailable"
    assert "private-file-payload" not in json.dumps(access.recent())


def test_attempts_include_only_retained_explicit_grants_for_same_runtime_call():
    access = DiagnosticAccess()
    common = {"runtime_id": "main-1", "session_id": "session-1", "run_id": "run-1",
              "model_call_id": "call-1", "agent": "main", "purpose": "agent"}
    access.remember(record("first-attempt", attempt=1, **common))
    access.remember(record("second-attempt", attempt=2, **common))
    access.remember(record("other-call", attempt=1, **{**common, "model_call_id": "call-2"}))
    access.remember(record("other-runtime", attempt=1, **{**common, "runtime_id": "child-1"}))
    result = json.loads(access.read("second-attempt", "attempts")["text"])
    assert result["scope"] == "retained_authorized_failures"
    assert [item["error_id"] for item in result["records"]] == ["first-attempt", "second-attempt"]


def test_related_reference_does_not_grant_access_and_copies_cannot_mutate_scope():
    access = DiagnosticAccess()
    access.remember({**record("outer"), "related_errors": [{"error_id": "inner", "message": "private-inner"}]})
    assert json.loads(access.read("outer", "related")["text"])["records"] == []
    assert access.read("inner")["status"] == "not_found"
    access.remember(record("inner", "explicitly shared inner"))
    related = json.loads(access.read("outer", "related")["text"])["records"]
    assert related[0]["message"] == "explicitly shared inner"
    access.get("inner")["message"] = "corrupted"
    access.recent()[0]["context"]["server"] = "forged"
    assert access.get("inner")["message"] == "explicitly shared inner"
    assert "server" not in access.get("outer")["context"]


def test_broken_lookup_returns_unavailable_without_recursive_failure(make_session, monkeypatch):
    session = make_session(refresh_models=False)
    tool = diagnostic_tool(session)
    access = collection(session.main)
    access.remember(record("original-error", "original-evidence"))
    emergency = []
    monkeypatch.setattr("virgo.features.diagnostics.emergency_write", emergency.append)

    def fail(*_args):
        raise OSError("evidence lookup failed")

    monkeypatch.setattr(access, "read", fail)
    assert query(tool, session.main, "original-error")["status"] == "unavailable"
    assert access.get("original-error")["message"] == "original-evidence"
    assert len(emergency) == 1 and emergency[0]["phase"] == "diagnostics.lookup"
