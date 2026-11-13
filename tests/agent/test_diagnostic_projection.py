"""Final tool observations preserve execution facts and survive view failures."""

from __future__ import annotations

import threading

import pytest

from virgo.agent import AgentConfig, AgentTool, Block, ToolResult
from virgo.agent.loop import _execute
from virgo.ai.providers import faux, openai
from virgo.ai.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolCall,
    ToolResultMessage,
    message_from_dict,
)
from virgo.harness.diagnostic_view import project_tool_result

from .test_loop import echo_tool, make_agent


def _result(agent):
    return next(message for message in agent.messages if isinstance(message, ToolResultMessage))


def test_projection_sees_final_hooks_and_recorded_evidence_once_in_provider_content():
    rendered = []

    def failure(*_):
        try:
            raise FileNotFoundError("required project file missing")
        except FileNotFoundError as cause:
            raise RuntimeError("operation could not finish") from cause

    def hook(*_):
        raise LookupError("result hook failed")

    def project(call, result):
        assert result.details["error"]["context"]["recorded"]
        assert result.details["related_errors"][0]["exception_type"] == "RuntimeError"
        rendered.append(call.id)
        return project_tool_result(call, result)

    tool = AgentTool("failing", "", {"type": "object"}, failure)
    agent, events = make_agent([tool], faux.reply(tools=[("failing", {}, "failure-call")]), faux.reply("recovered"),
                               after_tool_call=hook, project_tool_result=project)

    def record(event):
        if event.type == "tool_execution_end":
            event.result.details["error"]["context"]["recorded"] = True

    agent.subscribe(record)
    agent.prompt("go")
    assert rendered == ["failure-call"]
    tool_event = next(event for event in events if event.type == "tool_execution_end")
    assert "Diagnostic evidence" not in tool_event.result.as_text()
    message = _result(agent)
    assert message.is_error and message.text().count("[Diagnostic evidence;") == 1
    next_request = [message_from_dict(item) for item in faux.CALLS["loop"][1]["messages"]]
    payload = openai.convert_messages("", next_request)
    provider_result = next(item for item in payload if item["role"] == "tool")
    assert "LookupError" in provider_result["content"]
    assert message.details["error"]["error_id"] in provider_result["content"]
    assert "diagnostic_get" in provider_result["content"]
    assert "related_error_ids" in provider_result["content"]
    assert "traceback" not in provider_result["content"]


@pytest.mark.parametrize("stage", ["prepare", "preflight", "permission", "execute", "result_hook"])
def test_execution_facts_distinguish_failure_boundaries(stage):
    calls = []

    def fail(*_):
        raise RuntimeError("failure")

    def run(*_):
        calls.append("executed")
        return "created"

    tool = AgentTool("work", "", {"type": "object"}, run, read_only=True)
    config = {}
    if stage == "prepare":
        tool.prepare_arguments = fail
    elif stage == "execute":
        tool.execute = fail
    else:
        config[{"preflight": "before_tool_call", "permission": "before_tool_execute",
                "result_hook": "after_tool_call"}[stage]] = fail
    agent, _ = make_agent([tool], faux.reply(tools=[("work", {})]), faux.reply("next"), **config)
    agent.prompt("go")
    facts = _result(agent).details["execution"]
    if stage in {"prepare", "preflight", "permission"}:
        assert not calls
        assert facts == {"started": False, "outcome": "not_started", "side_effects": "none",
                         "result_hook": "not_run"}
    else:
        assert facts["started"] and facts["side_effects"] == "unknown"
        assert facts["outcome"] == ("unknown" if stage == "execute" else "returned_success")
        assert facts["result_hook"] == ("failed" if stage == "result_hook" else "not_run")


def test_permission_block_cannot_supply_forged_execution_or_dispatch():
    agent, _ = make_agent([echo_tool()], faux.reply(tools=[("echo", {})]), faux.reply("next"),
                          before_tool_execute=lambda _: Block("denied", details={
                              "execution": {"started": True, "outcome": "returned_success"},
                              "transport": {"dispatch": "sent"},
                          }))
    agent.prompt("go")
    result = _result(agent)
    assert result.details["execution"]["outcome"] == "not_started"
    assert "transport" not in result.details


def test_hooks_cannot_erase_or_override_execution_and_adapter_facts():
    transport = {"protocol": "mcp", "server": "local", "generation": 3, "dispatch": "sent",
                 "outcome": "unknown", "side_effects": "unknown"}

    def run(*_):
        return ToolResult.error("connection lost", details={"transport": dict(transport)})

    def recover(event, result):
        result.details["transport"]["dispatch"] = "not_started"
        return ToolResult.text("recovered", details={
            "execution": {"started": False, "side_effects": "none"},
            "transport": {"dispatch": "not_started", "side_effects": "none"},
        })

    agent, _ = make_agent([AgentTool("remote", "", {"type": "object"}, run, read_only=True)],
                          faux.reply(tools=[("remote", {})]), faux.reply("done"),
                          after_tool_call=recover, project_tool_result=project_tool_result)
    agent.prompt("go")
    result = _result(agent)
    assert not result.is_error and result.text() == "recovered"
    assert result.details["execution"] == {
        "started": True, "outcome": "returned_error", "side_effects": "unknown", "result_hook": "returned",
    }
    assert result.details["transport"] == transport
    assert result.details["related_errors"]


def test_successful_extension_list_details_keep_their_payload_contract():
    payload = [{"plugin_state": "retained"}, "extension metadata"]

    def hook(event, result):
        assert result.details is payload
        return result

    tool = AgentTool("extension", "", {"type": "object"}, lambda *_: ToolResult.text("done", details=payload))
    agent, events = make_agent([tool], faux.reply(tools=[("extension", {})]), faux.reply("finished"),
                               after_tool_call=hook, project_tool_result=project_tool_result)
    agent.prompt("go")
    result = _result(agent)
    event = next(event for event in events if event.type == "tool_execution_end")
    assert result.details is payload and event.result.details is payload
    assert result.text() == "done" and not result.is_error


@pytest.mark.parametrize("bad_return", [None, "wrong", [object()]])
def test_invalid_projection_keeps_original_result(bad_return):
    reports = []
    agent, _ = make_agent([echo_tool()], faux.reply(tools=[("echo", {"x": "original"})]), faux.reply("done"),
                          project_tool_result=lambda *_: bad_return, on_error=reports.append)
    agent.prompt("go")
    result = _result(agent)
    assert result.text() == "echo:original" and not result.is_error
    assert len(reports) == 1 and reports[0]["phase"] == "tool.projection"


def test_mutating_failed_projection_cannot_change_output_images_or_termination(monkeypatch):
    image = ImageContent("aW1hZ2U=", "image/png")
    original = ToolResult([TextContent("original"), image], details={"custom": "preserved"}, terminate=True)
    projected = []
    emergency = []

    def project(call, result):
        projected.append(call.id)
        call.arguments["value"] = "changed"
        result.content[0].text = "changed"
        result.content.clear()
        result.details.clear()
        result.is_error = True
        result.terminate = False
        raise ValueError("renderer failed")

    def report(_):
        raise OSError("diagnostic reporter unavailable")

    monkeypatch.setattr("virgo.agent.loop.emergency_write",
                        lambda details, reporting_error=None: emergency.append((details, reporting_error)))
    agent, _ = make_agent([AgentTool("render", "", {"type": "object"}, lambda *_: original)],
                          faux.reply(tools=[("render", {"value": "original"}, "render-call")]),
                          project_tool_result=project, on_error=report)
    agent.prompt("go")
    result = _result(agent)
    assert projected == ["render-call"] and len(faux.CALLS["loop"]) == 1
    assert result.content == [TextContent("original"), image] and not result.is_error
    assert original.terminate and original.details["custom"] == "preserved"
    assert agent.messages[1].tool_calls()[0].arguments == {"value": "original"}
    assert emergency[0][0]["phase"] == "tool.projection"
    assert isinstance(emergency[0][1], OSError)


def test_copy_failure_keeps_success_and_control_flags():
    class Opaque:
        def __deepcopy__(self, memo):
            raise RuntimeError("cannot copy opaque tool metadata")

    result = ToolResult.text("done", details={"opaque": Opaque()}, terminate=True)
    tool = AgentTool("opaque", "", {"type": "object"}, lambda *_: result)
    reports = []
    cfg = AgentConfig([tool], lambda *_: None, project_tool_result=project_tool_result, on_error=reports.append)
    call = ToolCall("id", "opaque", {})
    messages, terminate = _execute([call], AssistantMessage([call]), {"opaque": tool}, cfg,
                                   lambda _: None, threading.Event())
    assert terminate and messages[0].text() == "done" and not messages[0].is_error
    assert len(reports) == 1 and reports[0]["phase"] == "tool.projection"


def test_tool_refresh_applies_to_next_batch_without_replacing_inflight_tools():
    tools = [echo_tool("first", sequential=True), echo_tool("old")]
    replaced = False

    def refresh(event, result):
        nonlocal replaced
        if not replaced:
            replaced = True
            tools[:] = [echo_tool("new")]
        return result

    agent, _ = make_agent(tools, faux.reply(tools=[("first", {}), ("old", {})]),
                          faux.reply(tools=[("new", {})]), faux.reply("done"), after_tool_call=refresh)
    agent.prompt("go")
    results = [message for message in agent.messages if isinstance(message, ToolResultMessage)]
    assert [message.text() for message in results] == ["first:", "old:", "new:"]
    assert all(not message.is_error for message in results)
    assert faux.CALLS["loop"][1]["tools"] == ["new"]
    previous = faux.CALLS["loop"][1]["messages"]
    assert faux.CALLS["loop"][2]["messages"][:len(previous)] == previous
