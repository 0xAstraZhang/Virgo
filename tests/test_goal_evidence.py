from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from virgo.features import goal


def test_goal_keeps_full_verification_commands_and_tail_failures():
    command = "x" * 600 + "assert 'pages' in saved_json"
    output = "x" * 2500 + "FAIL: JSON has no pages"
    text = goal.transcript([
        AssistantMessage([ToolCall("check", "bash", {"command": command})]),
        ToolResultMessage("check", "bash", [TextContent(output)], is_error=True),
    ])
    assert command in text and output in text
    assert "id=check (error)" in text
    assert "TRUNCATED" not in text


def test_goal_truncation_is_explicit_bounded_and_keeps_latest_evidence():
    text = goal.transcript([
        *[UserMessage("old" * 8000) for _ in range(15)],
        ToolResultMessage("check", "bash", [TextContent("first\n" + "x" * 20000 + "\nFAIL: missing pages")]),
        AssistantMessage([TextContent("All files are complete.")]),
    ])
    assert len(text) <= goal.TRANSCRIPT_CHARS
    assert "earlier conversation omitted" in text
    assert "evidence omitted" in text
    assert "FAIL: missing pages" in text


@pytest.mark.parametrize("reply", [
    faux.reply('{"met":true,"reason":"done"}', stop_reason="length"),
    faux.reply('{"met":true,"reason":"done"}', stop_reason="aborted"),
    faux.reply('{"met":true,"reason":123}'),
    faux.reply('{"met":false,"reason":"blocked","impossible":"false"}'),
])
def test_goal_rejects_incomplete_or_malformed_verdicts(make_session, reply):
    session = make_session(reply)
    with pytest.raises(RuntimeError):
        goal.evaluate(session, "deliver files", [UserMessage("request")])
    assert not session.store.custom("goal_evaluation")
    assert len(session.store.custom("model_call")) == 1


def test_goal_evaluation_records_evidence_and_verdict(make_session):
    session = make_session(faux.reply('{"met":false,"reason":"read back the JSON"}'))
    verdict = goal.evaluate(session, "files complete", [UserMessage("request")])
    evidence = session.store.custom("goal_evaluation")[0]
    assert evidence["verdict"] == verdict
    assert not evidence["evidence_truncated"]
    assert len(evidence["evidence_sha256"]) == 64
    assert session.main.messages == []


def test_interrupt_during_goal_review_keeps_goal_active(make_session):
    session = make_session()

    def respond(context):
        if "completion evaluator" in context.system:
            session.abort()
            return faux.reply('{"met":true,"reason":"done"}')
        return faux.reply("done")

    faux.handler("test", respond)
    session.prompt("/goal verify artifacts")
    assert session.services["goal"]["goal"] is not None
    assert session.store.custom("goal")[-1]["status"] == "active"
