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
    pytest.param(("low", "high"), "high", "low", id="supports-low"),
    pytest.param(("medium", "high"), "high", "high", id="model-default"),
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
        faux.script("small", faux.reply('{"safe":true,"reason":"public file"}'))
        event = ToolCallEvent("review", "read", {"path": "/etc/hosts"}, session.tool("read"), "main", session.cwd)
        assert review_tool_call(session, event, "outside workspace") == (True, "public file")
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
