"""Diagnostic reporting remains useful when its input or its own output fails."""

from __future__ import annotations

import json
import re

import pytest

from virgo.diagnostics import capture_error, error_summary, safe_text, sanitize
from virgo.harness.diagnostics import DiagnosticLog


def test_native_exception_fields_cannot_be_hidden_by_broken_properties():
    class BrokenMetadata(RuntimeError):
        def __getattribute__(self, name):
            if name in {"__traceback__", "__cause__", "__context__", "__suppress_context__", "__class__"}:
                raise RuntimeError("diagnostic property failed")
            return super().__getattribute__(name)

    try:
        try:
            raise ValueError("underlying cause")
        except ValueError as cause:
            raise BrokenMetadata("original failure") from cause
    except BrokenMetadata as error:
        details = capture_error(error, phase="tool.execute")
    assert details["message"] == "original failure"
    assert details["exception_type"].endswith("BrokenMetadata")
    assert details["cause_chain"][1]["exception_type"] == "ValueError"
    assert "test_native_exception_fields" in details["traceback"]
    assert "diagnostic property failed" not in json.dumps(details)


def test_surrogate_secret_and_text_do_not_break_diagnostics():
    secret = "test-\udcff-secret"
    text = safe_text(f"failure with {secret}", secrets=(secret,))
    assert text == "failure with [redacted]"
    assert safe_text("filename=\udcff").encode("utf-8")
    details = capture_error(RuntimeError(secret), phase="test", secrets=(secret,))
    assert "secret" not in json.dumps(details)
    assert details["message"] == "[redacted]"


def test_cookie_headers_redact_every_cookie_without_losing_other_lines():
    message = ("Cookie: sid=private_one; session=private_two\n"
               "Set-Cookie: auth=private_three; Path=/; HttpOnly\n"
               "HTTP 503 in request abc")
    clean = safe_text(message)
    assert all(value not in clean for value in ("private_one", "private_two", "private_three"))
    assert "HTTP 503 in request abc" in clean
    clean = safe_text("Authorization=Bearer private_auth request failed after 3 seconds")
    assert "private_auth" not in clean and "request failed after 3 seconds" in clean


@pytest.mark.parametrize("secret", ["a", "e", "message", "context", "error_id"])
def test_explicit_task_label_keeps_diagnostic_schema_and_identity(secret, tmp_path):
    details = capture_error(RuntimeError(f"failed task '{secret}'"), phase="background.callable", secrets=(secret,))
    assert details["phase"] == "background.callable"
    assert details["message"] == "failed task '[redacted]'"
    assert re.fullmatch(r"[0-9a-f]{32}", details["error_id"])
    assert {"timestamp", "context", "cause_chain", "exception_type", "traceback"} <= details.keys()
    log = DiagnosticLog(tmp_path)
    saved = log.record(details)
    assert saved["error_id"] == details["error_id"]
    assert saved["phase"] == "background.callable"


def test_redacted_key_still_hides_its_sensitive_value():
    result = sanitize({"api_key": "unlisted-private-value"}, secrets=("api_key",))
    assert "unlisted-private-value" not in json.dumps(result)


def test_background_job_with_short_label_keeps_a_queryable_failure(make_session):
    session = make_session(refresh_models=False)

    def fail(signal):
        raise RuntimeError("job e failed")

    job = session.services["jobs"].start_callable("e", fail)
    assert job.done.wait(3)
    assert job.status == "failed"
    details = job.error_details
    assert re.fullmatch(r"[0-9a-f]{32}", details["error_id"])
    assert details["message"] == "job [redacted] failed"
    assert details["context"]["job_id"] == job.id
    assert session.diagnostics.recent()[0]["error_id"] == details["error_id"]


def test_bad_metadata_and_huge_integers_stay_json_serializable():
    class BrokenMap(dict):
        def items(self):
            raise ValueError("cannot enumerate")

    class BrokenValue:
        def __getattribute__(self, name):
            raise RuntimeError("cannot inspect")

    details = capture_error(RuntimeError("original"), phase="test", context={
        "bad": BrokenMap(), "value": BrokenValue(), "huge": 10 ** 10_000, "ordinary": "kept",
    })
    encoded = json.dumps(details, allow_nan=False)
    assert "original" in encoded and "kept" in encoded
    assert "unavailable" in encoded and "integer too large" in encoded


def test_shared_nested_metadata_has_a_global_output_budget():
    # Without a global budget, a small aliased object expands exponentially.
    nested = ["x" * 8192] * 64
    for _ in range(5):
        nested = [nested] * 64
    encoded = json.dumps(sanitize({"nested": nested}))
    assert len(encoded) < 400_000
    assert "omitted" in encoded or "size limit" in encoded


def test_invalid_error_id_is_repaired_without_losing_the_error(tmp_path):
    log = DiagnosticLog(tmp_path)
    saved = log.record({"error_id": [], "phase": "test", "message": "original failure"})
    assert isinstance(saved["error_id"], str) and saved["message"] == "original failure"
    assert json.loads(log.path.read_text())["error_id"] == saved["error_id"]


def test_log_preprocessing_failure_preserves_original_error_on_stderr(tmp_path, monkeypatch, capsys):
    log = DiagnosticLog(tmp_path)
    original = capture_error("original failure", phase="tool.execute")

    def fail(*args, **kwargs):
        raise RuntimeError("record sanitizer failed")

    monkeypatch.setattr("virgo.harness.diagnostics.sanitize", fail)
    saved = log.record(original)
    assert saved["error_id"] == original["error_id"]
    assert saved["message"] == "original failure"
    assert "record sanitizer failed" in saved["logging_error"]
    assert "original failure" in capsys.readouterr().err


@pytest.mark.parametrize("limit", [512, 1000, 4096])
def test_oversized_log_record_keeps_id_phase_and_cause_summary_within_budget(tmp_path, limit):
    try:
        try:
            raise ValueError("root cause " + "r" * 20_000)
        except ValueError as cause:
            raise RuntimeError("outer cause " + "o" * 50_000) from cause
    except RuntimeError as error:
        details = capture_error(error, phase="tool.execute")
    log = DiagnosticLog(tmp_path, max_bytes=limit)
    saved = log.record(details)
    assert log.path.stat().st_size <= limit
    record = json.loads(log.path.read_text())
    assert record["error_id"] == details["error_id"]
    assert record["phase"] == "tool.execute"
    assert record["record_truncated"]
    assert record["message"].startswith("outer cause")
    assert any(cause["message"].startswith("root cause") for cause in record["cause_chain"])
    assert saved["log_path"] == str(log.path)


def test_default_budget_keeps_full_diagnostic_stack(tmp_path):
    try:
        raise RuntimeError("ordinary failure")
    except RuntimeError as error:
        details = capture_error(error, phase="test")
    log = DiagnosticLog(tmp_path)
    saved = log.record(details)
    assert saved["traceback"] == details["traceback"]
    assert not saved.get("record_truncated")


def test_multilingual_record_obeys_escaped_json_byte_budget(tmp_path):
    try:
        try:
            raise ValueError("根因是服务器断开。" * 1000)
        except ValueError as cause:
            raise RuntimeError("模型调用失败，请查看诊断。" * 1000) from cause
    except RuntimeError as error:
        details = capture_error(error, phase="tool.execute")
    log = DiagnosticLog(tmp_path, max_bytes=512)
    saved = log.record(details)
    assert "logging_error" not in saved
    assert log.path.stat().st_size <= 512
    assert saved["message"].startswith("模型调用失败")
    assert saved["cause_chain"][0]["message"].startswith("根因是服务器断开")


def test_rotation_updates_cached_record_locations_and_marks_expired_logs(tmp_path):
    log = DiagnosticLog(tmp_path, max_bytes=1000, backups=1)
    original = capture_error("first " * 80, phase="test")
    first = log.record(original)
    log.record(capture_error("second " * 80, phase="test"))
    assert first["log_path"] == str(log.path.with_suffix(".jsonl.1"))
    assert log.record(original)["log_path"] == first["log_path"]
    log.record(capture_error("third " * 80, phase="test"))
    assert first["log_expired"] and "log_path" not in first
    assert first["log_pattern"] == str(log.path) + "*"
    assert "/diagnostics " + first["error_id"] in error_summary(first)


def test_zero_backups_still_enforces_rotation_budget(tmp_path):
    log = DiagnosticLog(tmp_path, max_bytes=1000, backups=0)
    first = log.record(capture_error("first " * 80, phase="test"))
    log.record(capture_error("second " * 80, phase="test"))
    assert log.path.stat().st_size <= 1000
    assert first["log_expired"]
    assert list(tmp_path.glob(log.path.name + "*")) == [log.path]


def test_unusable_log_budget_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="at least 512"):
        DiagnosticLog(tmp_path, max_bytes=1)
