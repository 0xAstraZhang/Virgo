"""Diagnostic boundaries retain failures without breaking loop recovery or cancellation."""

from __future__ import annotations

import json

import pytest

from virgo.agent import Agent, AgentConfig, AgentTool, Block, ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.diagnostics import capture_error

from .test_loop import echo_tool, make_agent


def fail(*args):
    try:
        raise ValueError("root failure")
    except ValueError as cause:
        raise RuntimeError("outer failure") from cause


@pytest.mark.parametrize("stage", ["prepare", "preflight", "permission", "execute", "result_hook"])
def test_tool_failures_capture_stage_cause_and_call_identity(stage):
    tool = echo_tool("failing")
    config = {}
    if stage == "prepare":
        tool.prepare_arguments = fail
    elif stage == "execute":
        tool.execute = fail
    else:
        field = {"preflight": "before_tool_call", "permission": "before_tool_execute",
                 "result_hook": "after_tool_call"}[stage]
        config[field] = fail
    agent, events = make_agent([tool], faux.reply(tools=[("failing", {"x": "private argument"}, "call-7")]),
                               faux.reply("recovered"), **config)
    agent.prompt("private prompt")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    diagnostic = result.details["error"]
    assert result.is_error and agent.last_text == "recovered"
    assert diagnostic["phase"] == f"tool.{stage}"
    assert diagnostic["exception_type"] == "RuntimeError"
    assert diagnostic["context"] == {"agent": "main", "tool_name": "failing", "tool_call_id": "call-7"}
    evidence = json.dumps(diagnostic)
    assert "ValueError" in evidence and "root failure" in evidence
    assert "test_diagnostics.py" in evidence
    assert "private argument" not in evidence and "private prompt" not in evidence
    assert [ev.tool_call_id for ev in events if ev.type == "tool_execution_end"] == ["call-7"]


def test_result_hook_failure_preserves_the_original_tool_failure():
    def failed_hook(*args):
        raise LookupError("hook failed")

    tool = AgentTool("failing", "", {"type": "object"}, fail)
    agent, _ = make_agent([tool], faux.reply(tools=[("failing", {})]), faux.reply("recovered"),
                          after_tool_call=failed_hook)
    agent.prompt("go")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert result.details["error"]["exception_type"] == "LookupError"
    assert result.details["related_errors"][0]["exception_type"] == "RuntimeError"
    assert result.details["related_errors"][0]["phase"] == "tool.execute"


def test_result_hook_recovery_keeps_original_diagnostic_without_marking_success_failed():
    tool = AgentTool("failing", "", {"type": "object"}, fail)
    agent, _ = make_agent([tool], faux.reply(tools=[("failing", {})]), faux.reply("done"),
                          after_tool_call=lambda *args: ToolResult.text("recovered by hook"))
    agent.prompt("go")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert not result.is_error and result.text() == "recovered by hook"
    assert result.details["related_errors"][0]["phase"] == "tool.execute"


def test_returned_tool_error_keeps_existing_metadata_and_adds_diagnostic():
    tool = AgentTool("failure", "", {"type": "object"},
                     lambda *args: ToolResult.error("command exited 7", details={"exit_code": 7}))
    agent, _ = make_agent([tool], faux.reply(tools=[("failure", {}, "call-9")]), faux.reply("done"))
    agent.prompt("go")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert result.details["exit_code"] == 7
    assert result.details["error"]["message"] == "command exited 7"
    assert not result.details["error"]["traceback"]


def test_diagnostic_size_limit_does_not_truncate_tool_result_content():
    output = "large failing command output\n" * 2000
    tool = AgentTool("failure", "", {"type": "object"}, lambda *args: ToolResult.error(output))
    agent, _ = make_agent([tool], faux.reply(tools=[("failure", {})]), faux.reply("done"))
    agent.prompt("go")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert result.text() == output
    assert len(result.details["error"]["message"]) < len(output)


@pytest.mark.parametrize("field", ["before_tool_call", "before_tool_execute"])
def test_permission_block_keeps_original_diagnostic(field):
    diagnostic = capture_error("policy unavailable", phase="permission.check")
    agent, _ = make_agent([echo_tool()], faux.reply(tools=[("echo", {})]), faux.reply("done"),
                          **{field: lambda *args: Block("denied", details={"error": diagnostic})})
    agent.prompt("go")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert result.is_error and result.details["error"] == diagnostic


def test_invalid_tool_return_becomes_error_without_losing_other_parallel_results():
    tool = AgentTool("invalid", "", {"type": "object"}, lambda *args: None)
    agent, _ = make_agent([tool, echo_tool()], faux.reply(tools=[("invalid", {}), ("echo", {"x": "ok"})]),
                          faux.reply("done"))
    agent.prompt("go")
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert results[0].is_error and results[0].details["error"]["exception_type"] == "TypeError"
    assert results[1].text() == "echo:ok" and not results[1].is_error


def test_listener_failure_is_reported_and_other_listeners_continue():
    reports = []
    agent, events = make_agent([], faux.reply("done"), on_error=reports.append)

    def broken(event):
        if event.type == "agent_start":
            raise RuntimeError("listener failed")

    observed = []
    agent.subscribe(broken)
    agent.subscribe(observed.append)
    agent.prompt("go")
    assert observed == events and agent.last_text == "done"
    assert len(reports) == 1
    assert reports[0]["phase"] == "agent.listener"
    assert reports[0]["context"]["event"] == "agent_start"
    assert "broken" in reports[0]["context"]["handler"]


def test_error_reporter_failure_uses_fallback_without_losing_original(monkeypatch):
    emergency = []
    monkeypatch.setattr("virgo.agent.agent.emergency_write", lambda details, reporting_error=None:
                        emergency.append((details, reporting_error)))
    agent, _ = make_agent([], faux.reply("done"), on_error=fail)

    def broken(event):
        if event.type == "agent_start":
            raise LookupError("original listener failure")

    agent.subscribe(broken)
    agent.prompt("go")
    assert agent.last_text == "done" and len(emergency) == 1
    assert emergency[0][0]["exception_type"] == "LookupError"
    assert isinstance(emergency[0][1], RuntimeError)


def test_sync_model_callback_failure_is_a_message_and_run_finishes():
    agent = Agent(AgentConfig(tools=[], call_model=fail))
    events = []
    agent.subscribe(events.append)
    agent.prompt("go")
    message = agent.messages[-1]
    assert message.stop_reason == "error"
    assert message.error_details["phase"] == "model.call"
    assert message.error_details["exception_type"] == "RuntimeError"
    assert not agent.running and events[-1].type == "agent_end"


def test_user_cancelled_tool_result_has_no_failure_diagnostic():
    def cancel(args, context):
        context.signal.set()
        return ToolResult.error("partial output\n\n[Command aborted]")

    tool = AgentTool("cancel", "", {"type": "object"}, cancel)
    agent, _ = make_agent([tool], faux.reply(tools=[("cancel", {})]))
    agent.prompt("go")
    result = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert "Command aborted" in result.text()
    assert "error" not in (result.details or {})
