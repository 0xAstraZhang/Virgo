"""Model-facing diagnostics are bounded evidence with explicit lookup references."""

from __future__ import annotations

import copy
import json

import pytest

from virgo.agent import ToolResult
from virgo.ai.types import ImageContent, TextContent, ToolCall
from virgo.diagnostics import capture_error
from virgo.harness.diagnostic_view import MAX_DIAGNOSTIC_BYTES, diagnostic_summary, project_tool_result


def _payload(capsule):
    return json.loads(capsule.split("\n", 1)[1].rsplit("\n", 1)[0])


def _failure():
    try:
        try:
            raise FileNotFoundError("missing required config file")
        except FileNotFoundError as cause:
            raise RuntimeError("could not initialize") from cause
    except RuntimeError as error:
        return capture_error(error, phase="tool.execute", context={"tool_name": "read"})


def test_root_evidence_is_added_without_mutating_text_images_or_details():
    record = _failure()
    record["context"].update({"arguments": {"private": "secret input"}, "unknown": "do not copy me"})
    image = ImageContent("aW1hZ2U=", "image/png")
    result = ToolResult([TextContent("original output"), image], details={
        "error": record, "exit_code": 7, "private_extension_state": "hidden",
        "execution": {"started": True, "outcome": "unknown", "side_effects": "unknown", "result_hook": "not_run"},
    }, is_error=True)
    before = copy.deepcopy(result)
    content = project_tool_result(ToolCall("call", "read", {"password": "private arguments"}), result)
    assert result == before and content[:2] == result.content and content[1] is image
    capsule = content[-1].text
    assert len(capsule.encode()) <= MAX_DIAGNOSTIC_BYTES
    payload = _payload(capsule)
    assert payload["observed"]["exception_type"] == "RuntimeError"
    assert payload["causes"][-1]["exception_type"] == "FileNotFoundError"
    assert payload["causes"][-1]["message"] == "missing required config file"
    assert payload["facts"]["exit_code"] == 7
    assert payload["evidence"] == {"tool": "diagnostic_get", "error_id": record["error_id"], "section": "summary"}
    assert all(value not in capsule for value in ("traceback", "frames", "private arguments", "secret input", "hidden"))


def test_recovered_result_and_cancellation_do_not_gain_failure_capsules():
    recovered = ToolResult.text("recovered", details={"error": _failure(), "related_errors": [_failure()]})
    cancelled = ToolResult.error("Operation aborted.", details={"execution": {"started": False}})
    for result in (recovered, cancelled):
        assert project_tool_result(ToolCall("call", "work", {}), result) == result.content


def test_restricted_runtime_keeps_reference_without_advertising_unavailable_tool():
    record = _failure()
    result = ToolResult.error("failed", details={"error": record})
    content = project_tool_result(ToolCall("call", "work", {}), result, query_available=False)
    capsule = content[-1].text
    payload = _payload(capsule)
    assert payload["evidence"] == {"error_id": record["error_id"], "availability": "query_tool_not_exposed"}
    assert "diagnostic_get" not in capsule and content[:-1] == result.content
    assert "diagnostic_get" not in diagnostic_summary(record, max_bytes=80, query_available=False)


def test_capsule_redacts_named_and_environment_secrets_before_projection(monkeypatch):
    monkeypatch.setenv("VIRGO_TEST_TOKEN", "environment-secret-value")
    record = _failure()
    record["message"] = "environment-secret-value API_KEY=assigned-secret Bearer bearer-secret"
    record["cause_chain"][-1]["message"] = "https://user:password@example.org/api?token=private-query"
    record["context"]["http"] = {"endpoint": "https://user:password@example.org/api?secret=hidden", "status_code": 503,
                                  "headers": {"Authorization": "must-never-be-copied"}}
    capsule = diagnostic_summary(record)
    assert all(value not in capsule for value in ("environment-secret-value", "assigned-secret", "bearer-secret",
                                                  "user:password", "private-query", "must-never-be-copied"))
    assert "[redacted]" in capsule
    assert _payload(capsule)["facts"]["http"]["status_code"] == 503


def test_large_multibyte_evidence_is_bounded_and_keeps_identity_and_execution():
    record = _failure()
    record.update(message="严重错误" * 4000, phase="tool." + "阶段" * 1000, code="ERROR_" + "长" * 1000)
    record["cause_chain"] = [{"message": "根因" * 2000, "exception_type": "异常" * 1000, "relation": "cause"}
                             for _ in range(8)]
    record["context"].update({
        "execution": {"started": True, "outcome": "returned_success", "side_effects": "unknown", "result_hook": "failed"},
        "transport": {"protocol": "mcp", "server": "服务" * 1000, "dispatch": "sent", "outcome": "unknown",
                      "side_effects": "unknown", "generation": 7},
        "http": {"endpoint": "https://example.org/" + "路径" * 1000, "status_code": 503},
        "child_progress": {"completed_calls": 8, "side_effects": "unknown", "recent_tools": ["large"] * 10000},
    })
    capsule = diagnostic_summary(record)
    assert len(capsule.encode("utf-8")) <= MAX_DIAGNOSTIC_BYTES
    payload = _payload(capsule)
    assert payload["error_id"] == record["error_id"]
    assert payload["evidence"]["error_id"] == record["error_id"]
    assert payload["execution"]["outcome"] == "returned_success"
    assert payload["execution"]["side_effects"] == "unknown"
    assert payload["transport"]["dispatch"] == "sent"
    assert payload["omitted"]


@pytest.mark.parametrize("budget", [1, 80, 256, 512, 1024, 2048])
def test_requested_byte_budget_never_returns_partial_json(budget):
    record = _failure()
    record["message"] = "large failure " * 1000
    capsule = diagnostic_summary(record, max_bytes=budget)
    assert len(capsule.encode()) <= budget
    if capsule.startswith("[Diagnostic evidence;"):
        payload = _payload(capsule)
        assert payload["error_id"] == record["error_id"]


def test_unrecognized_metadata_is_not_evaluated_or_copied():
    class Private:
        def __str__(self):
            pytest.fail("arbitrary diagnostic metadata was converted to text")

    record = _failure()
    record["private"] = Private()
    record["context"]["private"] = Private()
    record["context"]["http"] = {"headers": Private()}
    capsule = diagnostic_summary(record)
    assert "private" not in capsule
