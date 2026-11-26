"""Diagnostic references follow the query capability exposed to each runtime."""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.harness.extensions import ExtensionAPI

from tests.support.harness import calls, tool_results


def capsules(text):
    return [json.loads(value) for value in re.findall(
        r"\[Diagnostic evidence; treat as data\]\n(.*?)\n\[/Diagnostic evidence\]", text, re.DOTALL,
    )]


def request_text(request):
    text = []
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            text.append(content)
        else:
            text.extend(block.get("text", "") for block in content)
    return "\n".join(text)


def replace_query(session, name="inspect_failure", *, roles=("main",), declared=True):
    original = session.tool("diagnostic_get")
    assert original is not None
    session.remove_owned_tools("diagnostic_get")
    api = ExtensionAPI(session, "replacement_diagnostics")
    api.register_tool(replace(original, name=name), roles=roles)
    if declared:
        api.declare_diagnostic_query_tool(name)
    return api


def fail(*_args):
    raise OSError("fixture evidence remains queryable")


def test_renamed_query_reference_can_be_followed_by_the_model(make_session):
    def follow_reference(context):
        failure = next(message for message in reversed(context.messages) if isinstance(message, ToolResultMessage))
        reference = capsules(failure.text())[-1]["evidence"]
        return faux.reply(tools=[(reference["tool"], {
            "error_id": reference["error_id"], "section": reference["section"],
        })])

    session = make_session(
        faux.reply(tools=[("fixture_failure", {})]), follow_reference, faux.reply("inspected"),
        refresh_models=False,
    )
    replace_query(session)
    session.register_tool(AgentTool("fixture_failure", "fixture", {"type": "object"}, fail))
    session.prompt("Inspect the failed operation")

    failure, lookup = tool_results(session)
    assert failure.is_error
    reference = capsules(failure.text())[-1]["evidence"]
    assert reference["tool"] == "inspect_failure"
    assert reference["tool"] in calls()[1]["tools"] and "diagnostic_get" not in calls()[1]["tools"]
    assert not lookup.is_error and lookup.tool_name == reference["tool"]
    evidence = json.loads(lookup.text())
    assert evidence["status"] == "ok"
    assert "fixture evidence remains queryable" in evidence["text"]


def test_pending_evidence_uses_the_declared_query_name(make_session):
    session = make_session(faux.reply("inspected"), refresh_models=False)
    replace_query(session)
    record = session.main.report_error("pending observation", phase="fixture.pending")
    assert not calls()
    session.prompt("Inspect pending evidence")

    payload = next(item for item in capsules(request_text(calls()[-1])) if item["error_id"] == record["error_id"])
    assert payload["evidence"]["tool"] == "inspect_failure"
    assert len(calls()) == 1


def test_compaction_checkpoint_uses_the_declared_query_name(make_session):
    session = make_session(
        faux.reply("completed investigation " * 100), persist=True, refresh_models=False,
        settings={"compaction": {"keepRecentTokens": 1}},
    )
    replace_query(session)
    session.prompt("Investigate the fixture " * 100)
    record = session.main.report_error("retained observation", phase="fixture.retained", notify=False)
    faux.push("test", faux.reply("## Goal & Constraints\nContinue the fixture investigation."))
    assert session.compact() is not None
    faux.push("test", faux.reply("continued"))
    session.prompt("Continue")

    checkpoint = next(message.text() for message in session.main.messages
                      if getattr(message, "kind", None) == "notification"
                      and "Retained diagnostic references" in message.text())
    assert record["error_id"] in checkpoint
    assert "Use inspect_failure(error_id, section)" in checkpoint
    assert "diagnostic_get" not in checkpoint
    assert session.main.diagnostic_access.read(record["error_id"])["status"] == "ok"


def test_background_failure_uses_the_calling_runtime_query_name(make_session):
    session = make_session(refresh_models=False)
    replace_query(session)
    job = session.services["jobs"].start_callable("fixture", fail)
    assert job.done.wait(5) and job.status == "failed"
    faux.push("test", faux.reply(tools=[("job_output", {"id": job.id})]), faux.reply("inspected"))
    session.prompt("Inspect the background result")

    output = next(result for result in tool_results(session) if result.tool_name == "job_output")
    reference = capsules(output.text())[-1]["evidence"]
    assert reference == {"tool": "inspect_failure", "error_id": job.error_details["error_id"], "section": "summary"}
    assert reference["tool"] in calls()[-1]["tools"]


@pytest.mark.parametrize("unavailable", ["undeclared", "other-role", "runtime-restricted", "rolled-back"])
def test_unavailable_query_is_not_advertised(make_session, unavailable):
    session = make_session(faux.reply("inspected"), refresh_models=False)
    api = replace_query(
        session, roles=("subagent",) if unavailable == "other-role" else ("main", "subagent"),
        declared=unavailable != "undeclared",
    )
    if unavailable == "rolled-back":
        api.rollback()
    runtime = session.main
    if unavailable == "runtime-restricted":
        runtime = session.create_runtime("restricted", "subagent", tools=[])
    record = runtime.report_error("scoped observation", phase="fixture.scoped")
    runtime.agent.prompt("Inspect the observation")

    payload = next(item for item in capsules(request_text(calls()[-1])) if item["error_id"] == record["error_id"])
    assert payload["evidence"] == {"error_id": record["error_id"], "availability": "query_tool_not_exposed"}
    assert runtime.diagnostic_access.read(record["error_id"])["status"] == "ok"


def test_query_selection_tracks_exposed_tools_after_rollback(make_session):
    session = make_session(faux.reply("first"), faux.reply("second"), refresh_models=False)
    first = replace_query(session, "first_query")
    second = ExtensionAPI(session, "second_diagnostics")
    second.register_tool(replace(session.tool("first_query"), name="second_query"))
    second.declare_diagnostic_query_tool("second_query")

    for expected in ("first_query", "second_query"):
        record = session.main.report_error(f"observation for {expected}", phase="fixture.selection")
        session.prompt("Inspect the new observation")
        payload = next(item for item in capsules(request_text(calls()[-1])) if item["error_id"] == record["error_id"])
        assert payload["evidence"]["tool"] == expected
        assert expected in calls()[-1]["tools"]
        first.rollback()
