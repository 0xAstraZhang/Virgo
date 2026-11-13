"""CLI error boundaries preserve exit codes and point users to durable diagnostics."""

from __future__ import annotations

import io
import json
import threading
from importlib import import_module
from pathlib import Path

import pytest

from virgo.agent.types import AgentEvent, ToolResult
from virgo.cli import repl
from virgo.cli.console import PrintUI, event_json
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.controller import RunController
from virgo.diagnostics import capture_error

from tests.support.harness import ReplUI, ScriptedBroker, wait_until
from tests.support.tui import pump


def _records(directory):
    return [json.loads(line) for path in Path(directory).glob("*.jsonl") for line in path.read_text().splitlines()]


@pytest.mark.parametrize(("error_type", "code"), [(ValueError, 2), (PermissionError, 2), (RuntimeError, 1)])
def test_startup_failure_returns_nonzero_with_saved_traceback(monkeypatch, project, capsys, error_type, code):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))

    def broken(*args, **kwargs):
        raise error_type("startup unavailable")

    monkeypatch.setattr(cli, "AgentSession", broken)
    assert cli.main(["-p", "-C", str(project), "hello"]) == code
    output = capsys.readouterr()
    assert not output.out and "startup unavailable" in output.err
    records = _records(cli.Paths(project).user / "logs")
    assert len(records) == 1
    record = records[0]
    assert record["phase"] == "cli.startup" and record["exception_type"] == error_type.__name__
    assert "broken" in record["traceback"]
    assert record["error_id"] in output.err and record["log_path"] in output.err


def test_startup_reporting_failure_does_not_mask_original(monkeypatch, project, capsys):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))

    def broken(*args, **kwargs):
        raise RuntimeError("startup root cause")

    def cannot_log(*args, **kwargs):
        raise OSError("diagnostic destination unavailable")

    monkeypatch.setattr(cli, "AgentSession", broken)
    monkeypatch.setattr(cli.DiagnosticLog, "record", cannot_log)
    assert cli.main(["-p", "-C", str(project), "hello"]) == 1
    stderr = capsys.readouterr().err
    assert "startup root cause" in stderr and "diagnostic destination unavailable" in stderr


def test_print_command_failure_is_recorded_once_and_shutdown_runs(make_session, monkeypatch, capsys):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    session = make_session(ui=PrintUI())

    def broken(s, args):
        raise RuntimeError("command unavailable")

    session.commands.register("broken", broken)
    monkeypatch.setattr(cli, "AgentSession", lambda *args, **kwargs: session)
    assert cli.main(["-p", "/broken"]) == 1
    assert session._shutdown_started
    records = _records(session.paths.user / "logs")
    matches = [record for record in records if record["message"] == "command unavailable"]
    assert len(matches) == 1 and "broken" in matches[0]["traceback"]
    stderr = capsys.readouterr().err
    assert matches[0]["error_id"] in stderr and matches[0]["log_path"] in stderr


@pytest.mark.parametrize("interrupted", [False, True])
def test_shutdown_failure_cannot_replace_the_original_exit_code(make_session, monkeypatch, capsys, interrupted):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    session = make_session(ui=PrintUI())

    def broken_prompt(*args):
        if interrupted:
            raise KeyboardInterrupt
        raise RuntimeError("run failed first")

    def broken_shutdown():
        raise OSError("shutdown failed second")

    monkeypatch.setattr(session, "prompt", broken_prompt)
    monkeypatch.setattr(cli, "AgentSession", lambda *args, **kwargs: session)
    # Restore shutdown before the fixture's own cleanup.
    with monkeypatch.context() as patch:
        patch.setattr(session, "shutdown", broken_shutdown)
        assert cli.main(["-p", "hello"]) == (130 if interrupted else 1)
    records = _records(session.paths.user / "logs")
    assert {row["phase"] for row in records} == ({"session.shutdown"} if interrupted else {
        "cli.run", "session.shutdown",
    })
    assert "shutdown failed second" in capsys.readouterr().err


def test_repl_inline_command_keeps_running_and_saves_error(make_session, monkeypatch):
    monkeypatch.setattr(repl, "_setup_readline", lambda *args: None)
    ui, broker = ReplUI(), ScriptedBroker()
    session = make_session(ui=ui)

    def broken(s, args):
        raise RuntimeError("status lookup failed")

    session.commands.register("status", broken, concurrent=True)
    runner = threading.Thread(target=repl.run_repl, args=(session, ui, broker), daemon=True)
    runner.start()
    try:
        broker.push("line", "/status")
        wait_until(lambda: "status lookup failed" in ui.text)
        assert runner.is_alive()
        records = _records(session.paths.user / "logs")
        record = next(row for row in records if row["phase"] == "cli.command")
        assert record["context"]["command"] == "status" and record["error_id"] in ui.text
    finally:
        broker.push("line", "/exit")
        runner.join(5)
    assert not runner.is_alive()


def test_tui_worker_failure_is_saved_and_controller_becomes_idle(make_session):
    ui = FullscreenUI()
    session = make_session(ui=ui)
    controller = RunController(session, ui)

    def broken():
        raise RuntimeError("fullscreen command failed")

    controller._launch(broken)
    updates = pump(controller)
    assert not controller.busy
    assert any(update.kind == "done" and update.payload.failed for update in updates)
    records = _records(session.paths.user / "logs")
    record = next(row for row in records if row["phase"] == "cli.tui_worker")
    assert "broken" in record["traceback"] and record["context"]["ticket"] == 1
    assert any(update.kind == "print" and record["error_id"] in update.payload[0] for update in updates)


def test_tui_concurrent_command_error_retains_command_name(make_session):
    ui = FullscreenUI()
    session = make_session(ui=ui)
    controller = RunController(session, ui)

    def broken(s, args):
        raise RuntimeError("concurrent command unavailable")

    session.commands.register("status", broken, concurrent=True)
    controller.submit("/status private argument")
    pump(controller, lambda item: not item._helpers)
    records = _records(session.paths.user / "logs")
    record = next(row for row in records if row["phase"] == "cli.command")
    assert record["context"]["command"] == "status"
    assert "private argument" not in json.dumps(record)


def test_tool_json_events_expose_diagnostics_without_unrelated_details():
    primary = capture_error("tool execution failed", phase="tool.execute")
    related = capture_error("result hook failed", phase="tool.result_hook")
    result = ToolResult.error("failed", details={"error": primary, "related_errors": [related],
                                                "private_metadata": "not part of the event"})
    event = event_json(AgentEvent("tool_execution_end", tool_name="custom", tool_call_id="call-1",
                                 result=result, is_error=True))
    assert event["error_details"] == primary and event["related_errors"] == [related]
    assert "not part of the event" not in json.dumps(event)


def test_tool_json_event_redacts_custom_extension_error_metadata():
    result = ToolResult.error("failed", details={
        "error": {"message": "Authorization: Bearer confidential-token", "api_key": "credential-value"},
    })
    event = event_json(AgentEvent("tool_execution_end", result=result, is_error=True))
    assert "confidential-token" not in json.dumps(event) and "credential-value" not in json.dumps(event)
