"""All auxiliary calls use the selected small model's reasoning capabilities."""

from __future__ import annotations

import json

import pytest

from virgo import ai
from virgo.agent.types import ToolCallEvent
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, UserMessage
from virgo.features.goal import evaluate
from virgo.features.permission import review_tool_call


@pytest.mark.parametrize("levels,default,expected", [
    pytest.param(("low", "medium", "high"), "high", "low", id="supports-low"),
    pytest.param(("low", "high", "max"), None, "low", id="compatible-low-without-default"),
    pytest.param(("high", "max"), "high", "high", id="model-default"),
    pytest.param(None, None, None, id="server-default"),
])
@pytest.mark.parametrize("consumer", ["permission", "goal", "compaction", "memory-tidy", "memory-extract"])
def test_small_model_consumers_use_model_effort(make_session, monkeypatch, consumer, levels, default, expected):
    session = make_session(settings={"compaction": {"keepRecentTokens": 1}})
    session.effort = "max"
    session.small_model = session.resolve("faux/small")
    session.small_model.reasoning = True
    session.small_model.supported_reasoning_levels = levels
    session.small_model.default_reasoning_level = default
    requests = []
    stream = ai.stream

    def record_stream(model, context, options):
        requests.append((model.spec, options.effort, context.tools))
        return stream(model, context, options)

    monkeypatch.setattr(ai, "stream", record_stream)
    if consumer == "permission":
        faux.script("small", faux.reply(json.dumps({
            "decision": "allow", "reason": "public file", "risk": "low", "authorization": "implicit",
            "evidence_ids": [],
        })))
        event = ToolCallEvent("review", "read", {"path": "/etc/hosts"}, session.tool("read"), "main", session.cwd)
        verdict = review_tool_call(session, event, "outside workspace")
        assert verdict.decision == "allow"
        assert verdict.reason == "public file"
    elif consumer == "goal":
        faux.script("small", faux.reply('{"met":true,"reason":"tests passed","impossible":false}'))
        verdict = evaluate(session, "tests pass", [UserMessage("Tests: 3 passed")])
        assert verdict["met"] is True
    elif consumer == "compaction":
        session.main.messages.extend([
            UserMessage("Earlier request"),
            AssistantMessage([TextContent("Earlier work. " * 1000)]),
            UserMessage("Current request"),
            AssistantMessage([TextContent("Current work. " * 100)]),
        ])
        faux.script("small", faux.reply("## Goal\nFinish the current request."))
        assert session.compact() is not None
        assert session.main.messages[0].kind == "summary"
    elif consumer == "memory-tidy":
        memory = session.services["memory"]
        memory.write("First", "project", "First fact", "Use pytest.")
        memory.write("Second", "project", "Second fact", "Run tests before delivery.")
        faux.script("small", faux.reply(json.dumps([
            {"name": "Testing", "type": "project", "description": "Test policy", "body": "Run pytest before delivery."},
        ])))
        session.prompt("/memory tidy")
        assert [item.name for item in memory.all()] == ["Testing"]
    else:
        session.settings.set("memory.autoExtract", True)
        session.main.messages.append(UserMessage("Remember to use pytest for project tests."))
        faux.script("small", faux.reply(json.dumps([
            {"name": "Testing", "type": "project", "description": "Test runner", "body": "Use pytest."},
        ])))
        session.shutdown()
        assert session.services["memory"].find("Testing") is not None

    assert requests == [("faux/small", expected, [])]
    assert session.effort == "max"
    records = session.store.custom("model_call")
    assert len(records) == 1
    assert records[0]["purpose"] == consumer
    assert records[0]["model"] == "faux/small"
    assert records[0]["effort"] == expected
    assert records[0]["usage"]["input"] > 0


@pytest.mark.parametrize("limit,model_max,expected", [
    pytest.param(512, 32_768, 512 + ai.REASONING_HEADROOM, id="headroom-for-hidden-reasoning"),
    pytest.param(8000, 10_000, 10_000, id="capped-by-model-maximum"),
    pytest.param(8000, 4096, 8000, id="never-below-the-request"),
    pytest.param(None, 32_768, None, id="no-limit-requested"),
])
def test_one_shot_completions_reserve_reasoning_headroom(monkeypatch, limit, model_max, expected):
    # Providers count reasoning against the output limit: a 512-token verdict budget was spent
    # entirely on thinking, so the permission reviewer never produced a decision.
    faux.script("small", faux.reply("ok"))
    model = ai.resolve_model("faux/small")
    model.max_tokens = model_max
    limits = []
    stream = ai.stream

    def record_stream(model, context, options):
        limits.append(options.max_tokens)
        return stream(model, context, options)

    monkeypatch.setattr(ai, "stream", record_stream)
    assert ai.complete(model, "", "hi", max_tokens=limit).text() == "ok"
    assert limits == [expected]
