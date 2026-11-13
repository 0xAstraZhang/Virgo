"""Extensions retain their recovery semantics and leave enough evidence to debug failures."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from virgo.harness.extensions import EventBus, _run_hook


def test_handler_failure_retains_cause_and_dispatch_context_without_interrupting_handlers():
    seen, records = [], []
    marker = object()
    bus = EventBus(on_diagnostic=lambda details, args: records.append((details, args)))

    def broken(value):
        try:
            raise ValueError("missing extension configuration")
        except ValueError as cause:
            raise RuntimeError("cannot prepare context") from cause

    bus.on("context", broken)
    bus.on("context", lambda value: seen.append(value))
    assert bus.emit("context", marker) == []
    assert seen == [marker] and len(records) == 1
    details, args = records[0]
    assert args == (marker,)
    assert details["phase"] == "extension.handler"
    assert details["context"]["event"] == "context"
    assert details["context"]["handler"].endswith("broken")
    assert details["exception_type"] == "RuntimeError"
    assert [node["exception_type"] for node in details["cause_chain"]] == ["RuntimeError", "ValueError"]
    assert "broken" in details["traceback"]


@pytest.mark.parametrize("diagnostic_callback", [False, True])
def test_broken_reporter_does_not_stop_dispatch(diagnostic_callback, capsys):
    seen = []

    def fail_reporting(*args):
        raise OSError("reporting sink unavailable")

    bus = EventBus(on_diagnostic=fail_reporting) if diagnostic_callback else EventBus(on_error=fail_reporting)

    def broken():
        raise RuntimeError("original extension failure")

    bus.on("input", broken)
    bus.on("input", lambda: seen.append("still called"))
    bus.emit("input")
    assert seen == ["still called"]
    stderr = capsys.readouterr().err
    assert "original extension failure" in stderr and "reporting sink unavailable" in stderr
    assert "traceback" in stderr and "broken" in stderr


def test_standalone_event_bus_reports_traceback_without_debug_flag(monkeypatch, capsys):
    monkeypatch.delenv("VIRGO_DEBUG", raising=False)
    messages = []
    bus = EventBus(on_error=messages.append)

    def broken():
        raise RuntimeError("bad extension")

    bus.on("event", broken)
    bus.emit("event")
    assert "bad extension" in messages[0]
    assert '"traceback"' in capsys.readouterr().err


def test_extension_import_failure_is_saved_and_startup_continues(make_session, project):
    path = project / ".virgo" / "extensions" / "broken.py"
    path.parent.mkdir(parents=True)
    path.write_text('raise RuntimeError("extension import unavailable")\n')
    session = make_session()
    records = [json.loads(line) for path in (session.paths.user / "logs").glob("*.jsonl")
               for line in path.read_text().splitlines()]
    record = next(row for row in records if row["phase"] == "extension.load")
    assert record["context"]["extension"] == str(path)
    assert record["exception_type"] == "RuntimeError" and "<module>" in record["traceback"]
    assert record["error_id"] in session.ui.text
    assert str(session.paths.user / "logs") in session.ui.text


@pytest.mark.parametrize("exit_code", [0, 2, 3, -9])
def test_command_hook_exit_status_has_context_but_not_private_payload(monkeypatch, project, exit_code):
    records = []
    monkeypatch.setattr("virgo.harness.extensions.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=exit_code, stdout="private-output", stderr="missing executable",
    ))
    payload = {"hook_event_name": "PreToolUse", "session_id": "session-1", "agent": "worker",
               "tool_call_id": "call-1", "tool_name": "write", "tool_input": {"content": "private-input"}}
    result = _run_hook({"command": "command-secret"}, payload, project, on_error=records.append, hook_index=2)
    assert result == (exit_code, "private-output", "missing executable")
    if exit_code in (0, 2):
        assert not records
        return
    details = records[0]
    assert details["code"] == "HOOK_EXIT_FAILED"
    assert details["context"]["hook_index"] == 2
    assert details["context"]["tool_call_id"] == "call-1"
    assert details["context"]["exit_code"] == exit_code
    raw = json.dumps(details)
    assert all(value not in raw for value in ("command-secret", "private-input", "private-output"))


def test_hook_timeout_keeps_stack_without_embedding_command(monkeypatch, project):
    records = []

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("command-with-private-credentials", kwargs["timeout"],
                                        output="private stdout", stderr="private stderr")

    monkeypatch.setattr("virgo.harness.extensions.subprocess.run", timeout)
    assert _run_hook({"command": "command-with-private-credentials", "timeout": 0.5},
                     {"hook_event_name": "Stop"}, project, on_error=records.append) == (1, "", "hook timed out")
    details = records[0]
    assert details["code"] == "HOOK_TIMEOUT" and details["context"]["timeout_seconds"] == 0.5
    assert "_run_hook" in details["traceback"] and "timeout" in details["traceback"]
    assert all(value not in json.dumps(details) for value in (
        "command-with-private-credentials", "private stdout", "private stderr",
    ))


def test_broken_hook_reporter_preserves_original_failure(monkeypatch, project, capsys):
    def broken(*args, **kwargs):
        raise OSError("hook execution unavailable")

    def broken_reporter(details):
        raise RuntimeError("reporter unavailable")

    monkeypatch.setattr("virgo.harness.extensions.subprocess.run", broken)
    assert _run_hook({"command": "private command"}, {}, project, on_error=broken_reporter)[0] == 1
    stderr = capsys.readouterr().err
    assert "hook execution unavailable" in stderr and "reporter unavailable" in stderr
