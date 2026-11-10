from __future__ import annotations

from virgo.ai.providers import faux
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from virgo.harness.compaction import find_cut
from virgo.harness.session import AgentSession

from .conftest import RecordingUI, calls


def summarizer(ctx):
    if "context summarization assistant" in ctx.system:
        return faux.reply("## Goal\nbuild the thing\n## Next Steps\n1. finish")
    return None


def scripted(*replies):
    """Responder: summary requests get a summary; other calls consume `replies` in order."""
    queue = list(replies)

    def respond(ctx):
        return summarizer(ctx) or queue.pop(0)

    return respond


def test_find_cut_never_splits_tool_pairs():
    msgs = [
        UserMessage("x" * 400),
        AssistantMessage([ToolCall("1", "read", {"path": "a"})]),
        ToolResultMessage("1", "read", [TextContent("y" * 4000)]),
        AssistantMessage([TextContent("z" * 40)]),
    ]
    cut = find_cut(msgs, keep_tokens=500)
    assert not isinstance(msgs[cut], ToolResultMessage)


def test_manual_compaction_keeps_recent_and_strips_thinking(make_session, project):
    s = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 10}})
    big = "data " * 2000
    for i in range(3):
        resp = scripted(faux.reply(f"answer {i} {big}", thinking="secret reasoning"))
        faux.push("test", resp)
        s.prompt(f"question {i}")
    faux.push("test", scripted())
    res = s.compact(focus="the API")
    assert res is not None and res["tokens_before"] > 0
    msgs = s.main.messages
    assert msgs[0].kind == "summary" and "build the thing" in msgs[0].text()
    assert not any(
        isinstance(b, ThinkingContent) for m in msgs if isinstance(m, AssistantMessage) for b in m.content
    )
    assert any(
        isinstance(m, UserMessage) and m.text() == "question 2" for m in msgs
    )  # current request pinned/kept
    # the summarizer saw the focus and the transcript
    summary_req = [c for c in calls() if "context summarization assistant" in c["system"]][0]
    assert (
        "the API" in summary_req["messages"][0]["content"]
        and "question 0" in summary_req["messages"][0]["content"]
    )
    # reloading the session file rebuilds the same compacted context
    s2 = AgentSession(
        project, ui=RecordingUI(), model="faux/test", session=s.store.path, permission_mode="yolo"
    )
    assert [m.text() if hasattr(m, "text") else None for m in s2.main.messages] == [
        m.text() if hasattr(m, "text") else None for m in msgs
    ]
    s2.shutdown()


def test_auto_compaction_triggers_before_model_call(make_session):
    s = make_session(settings={"compaction": {"triggerTokens": 300, "keepRecentTokens": 50}})
    faux.push("test", scripted(faux.reply("x " * 1500)))
    s.prompt("first")
    faux.push("test", scripted(), scripted(faux.reply("after compaction")))  # summary call comes first
    s.prompt("second")
    assert s.main.messages[0].kind == "summary"
    assert s.main.agent.last_text == "after compaction"


def test_reactive_compaction_on_overflow(make_session):
    s = make_session()
    faux.push("test", scripted(faux.reply("long " * 2000)))
    s.prompt("first")
    faux.push(
        "test",
        faux.error("prompt is too long: 2000000 tokens > 1000000", kind="overflow"),
        scripted(),
        scripted(faux.reply("fits now")),
    )
    s.prompt("second")
    assert s.main.agent.last_text == "fits now"
    assert s.main.messages[0].kind == "summary"
