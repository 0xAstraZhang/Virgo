"""Summary compaction: where to cut, size estimates, and manual, automatic and reactive runs."""

from __future__ import annotations

from dataclasses import replace

import pytest

from virgo.agent import AgentEvent, AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from virgo.harness.compaction import estimate_tokens, find_cut
from virgo.harness.session import AgentSession
from virgo.harness.store import SessionStore

from tests.support.harness import RecordingUI, calls, install_responses


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


def test_estimate_includes_system_and_tool_definitions_without_usage():
    messages = [UserMessage("x" * 40)]
    tool = ToolSpec("example", "description", {"type": "object"})
    assert estimate_tokens(messages, system="s" * 400) == 110
    with_tools = estimate_tokens(messages, system="s" * 400, tools=[tool])
    assert with_tools > 110
    tool.description += "t" * 400
    assert estimate_tokens(messages, system="s" * 400, tools=[tool]) == with_tools + 100


def test_reported_usage_already_includes_system_and_cached_input():
    messages = [AssistantMessage(usage=Usage(input=100, output=20, cache_read=60, cache_write=20)),
                UserMessage("next" * 4)]
    assert estimate_tokens(messages, system="s" * 4000) == 204
    # A compaction or context reset invalidates the old request's usage.
    assert estimate_tokens(messages, valid_from=1, system="s" * 4000) == 1004


def test_runtime_estimate_counts_project_instructions_and_tools_without_request_hooks(make_session, project, monkeypatch):
    (project / "AGENTS.md").write_text("project rules " * 100, encoding="utf-8")
    session = make_session()
    runtime = session.main
    monkeypatch.setattr(session.bus, "chain", lambda *a: pytest.fail("status invoked a context hook"))
    monkeypatch.setattr(session.services["web"], "prepare", lambda *a: pytest.fail("status prepared a request"))
    assert ("project rules " * 100).strip() in runtime.system
    baseline = runtime.context_tokens()
    assert baseline > len(runtime.system) // 4 > 0
    runtime.system += "s" * 400
    assert runtime.context_tokens() == baseline + 100
    runtime.tools = [*runtime.tools, AgentTool("example", "t" * 400, {"type": "object"}, lambda *a: None)]
    assert runtime.context_tokens() > baseline + 200
    assert session.store.total_usage() == dict(input=0, output=0, cache_read=0, cache_write=0)


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
        project, ui=RecordingUI(), model="faux/test", session=s.store.path, approval_policy="never"
    )
    assert [m.text() if hasattr(m, "text") else None for m in s2.main.messages] == [
        m.text() if hasattr(m, "text") else None for m in msgs
    ]
    s2.shutdown()


def test_auto_compaction_triggers_before_model_call(make_session):
    s = make_session(settings={"compaction": {"keepRecentTokens": 50}})
    faux.push("test", scripted(faux.reply("x " * 1500)))
    s.prompt("first")
    s.settings.set("compaction.triggerTokens", 300)
    faux.push("test", scripted(), scripted(faux.reply("after compaction")))  # summary call comes first
    s.prompt("second")
    assert s.main.messages[0].kind == "summary"
    assert s.main.agent.last_text == "after compaction"


@pytest.mark.parametrize("usage", [
    pytest.param(Usage(input=4800, output=150), id="below-trigger"),
    pytest.param(Usage(input=800, cache_read=4000, cache_write=400, output=150), id="cached-above-trigger"),
])
def test_auto_compaction_waits_for_growth_from_first_reported_input(make_session, project, monkeypatch, usage):
    previous = make_session(faux.reply("started"), faux.reply("previous progress " * 1600), persist=True)
    previous.prompt("Start the task.")
    assert previous.main.compact(reason="auto")
    previous.shutdown()
    session = make_session(session="continue", settings={"compaction": {"triggerTokens": 5000, "keepRecentTokens": 250}})
    runtime = session.main
    assert runtime.compacted_at == 1
    (project / "small.txt").write_text("s" * 200)
    (project / "large.txt").write_text("L" * 4000)
    summary = AssistantMessage([TextContent("progress " * 500)])
    install_responses(
        monkeypatch, summary,
        AssistantMessage([ToolCall("1", "read", {"path": "small.txt"})], provider="faux", model="test",
                         usage=usage, stop_reason="tool_use"),
        AssistantMessage([ToolCall("2", "read", {"path": "large.txt"})], provider="faux", model="test",
                         usage=Usage(input=usage.input_total + 200, output=150), stop_reason="tool_use"),
        summary, AssistantMessage([TextContent("done")]),
    )
    estimates, decisions = [], []

    def after_turn(event, rt):
        if event.tool_results:
            decisions.append(rt._compaction_due())

    session.bus.on("compact", lambda rt, _: estimates.append(rt.context_tokens(estimate_only=True)))
    session.bus.on("turn_end", after_turn)
    session.prompt("Read both files.")

    assert estimates[0] < 5000
    assert decisions == [False, True]
    assert len(estimates) == 2 and runtime.agent.last_text == "done"


@pytest.mark.parametrize("window, baseline, floor, due_at", [
    pytest.param(16_000, 12_000, 14_400, 14_400, id="small-window"),
    pytest.param(16_000, 5_000, 9_000, 12_800, id="below-trigger"),
    pytest.param(200_000, 190_000, 195_904, 195_904, id="near-large-window-limit"),
])
def test_auto_compaction_growth_respects_context_budget(make_session, monkeypatch, window, baseline, floor, due_at):
    session = make_session(faux.reply("started"), faux.reply("summary"), settings={
        "providers": {"faux": {"models": {"test": {"contextWindow": window}}}},
    })
    session.prompt("Start the task.")
    runtime = session.main
    assert runtime.compact(reason="auto")
    install_responses(monkeypatch, AssistantMessage(provider="faux", model="test", usage=Usage(input=baseline)))
    session.prompt("Continue.")

    assert runtime._auto_compaction_floor == floor
    assert not runtime._compaction_due()
    usage = runtime.messages[-1].usage
    usage.output = due_at - baseline - 1
    assert not runtime._compaction_due()
    usage.output += 1
    assert runtime._compaction_due()
    usage.output = window + 1 - baseline
    assert runtime._compaction_due()


@pytest.mark.parametrize("settings, due_at", [
    pytest.param({}, 14_400, id="default-ratio"),
    pytest.param({"triggerRatio": 0.95}, 15_200, id="configured-ratio"),
    pytest.param({"triggerTokens": 15_000}, 15_000, id="configured-tokens"),
    pytest.param({"triggerTokens": 20_000}, 20_000, id="deferred-past-window"),
])
def test_auto_compaction_caps_estimated_floor_and_respects_trigger(make_session, monkeypatch, settings, due_at):
    session = make_session(faux.reply("started"), faux.reply("summary"), settings={
        "compaction": settings,
    })
    session.prompt("Start the task.")
    runtime = session.main
    target = replace(session.resolve("faux/fallback"), context_window=16_000)
    tokens = 15_900
    monkeypatch.setattr(runtime, "context_tokens", lambda *, estimate_only=False: tokens)

    assert runtime.compact(reason="auto", model=target)
    assert runtime._auto_compaction_floor == 14_400
    assert runtime._compaction_due(target) == (tokens >= due_at)
    tokens = due_at - 1
    assert not runtime._compaction_due(target)
    tokens = due_at
    assert runtime._compaction_due(target)


@pytest.mark.parametrize("switch", ["set_model", "window_only", "fallback"])
def test_auto_compaction_floor_resets_on_model_switch(make_session, monkeypatch, switch):
    session = make_session(faux.reply("started"), faux.reply("summary"), settings={
        "providers": {"faux": {"models": {"small": {"contextWindow": 16_000}}}},
    })
    runtime = session.main
    session.prompt("Start the task.")
    assert runtime.compact(reason="auto")
    install_responses(monkeypatch, AssistantMessage(
        provider="faux", model="test", usage=Usage(input=12_000, output=5000),
    ))
    session.prompt("Continue.")
    assert runtime.context_tokens() == 17_000
    assert runtime._auto_compaction_floor == 32_000

    if switch == "set_model":
        session.set_model("faux/small")
        assert runtime._compaction_due()
    elif switch == "window_only":
        runtime.model = replace(runtime.model, context_window=16_000)
        assert runtime._compaction_due()
    else:
        assert runtime._compaction_due(session.resolve("faux/small"))


@pytest.mark.parametrize("window, expected_floor", [(200_000, 32_000), (16_000, 14_400)])
def test_post_compaction_usage_belongs_to_target_model(make_session, window, expected_floor):
    session = make_session(faux.reply("started"), faux.reply("summary"))
    session.prompt("Start the task.")
    runtime = session.main
    fallback = replace(session.resolve("faux/fallback"), context_window=window)
    assert runtime.compact(reason="auto", model=fallback)
    estimate_floor = runtime._auto_compaction_floor

    def report(model):
        reply = AssistantMessage(provider=model.provider, model=model.id, usage=Usage(input=12_000))
        runtime._on_event(AgentEvent("message_end", "main", message=reply))

    report(runtime.model)
    assert runtime._auto_compaction_floor == estimate_floor
    report(fallback)
    assert runtime._auto_compaction_floor == expected_floor
    session.settings.set("compaction.triggerTokens", 1)
    assert not runtime._compaction_due(fallback)
    assert runtime._compaction_due()  # Switching back to the primary model clears the floor.


def test_retry_compacts_portable_history_before_smaller_model_call(make_session, monkeypatch):
    session = make_session(
        faux.reply("started"), faux.reply("initial summary"),
        faux.error("overloaded", kind="retryable"), faux.error("overloaded", kind="retryable"),
        faux.reply("fallback summary"), settings={
            "providers": {"faux": {"models": {"small": {"contextWindow": 16_000}}}},
            "retry": {"fallbackModel": "faux/small"},
        },
    )
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)
    session.prompt("Start the task.")
    assert session.main.compact(reason="auto")
    faux.push("small", faux.reply("done"))
    session.prompt("x" * 68_000)
    assert session.main.agent.last_text == "done"
    assert "fallback summary" in calls("small")[0]["messages"][0]["content"]


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


def test_serialization_keeps_complete_tool_arguments_and_tail_evidence_within_budget():
    from virgo.harness.compaction import serialize

    command = "x" * 800 + " verify --output /tmp/complete-result.json"
    output = "progress\n" * 1000 + "ERROR: missing entry\nFull output: /tmp/full-log.txt"
    text = serialize([
        AssistantMessage([ToolCall("check", "bash", {"command": command})]),
        ToolResultMessage("check", "bash", [TextContent(output)], is_error=True),
    ])
    assert command in text and output in text
    assert "(error)" in text and "omitted" not in text


def test_serialization_applies_only_the_total_budget_with_explicit_omission(monkeypatch):
    from virgo.harness import compaction

    monkeypatch.setattr(compaction, "MAX_INPUT_CHARS", 6000)
    text = compaction.serialize([
        UserMessage("original request"),
        AssistantMessage([ToolCall("large", "write", {"content": "x" * 20_000})]),
        ToolResultMessage("large", "write", [TextContent("ERROR at end\nOutput: /tmp/result.txt")]),
    ])
    assert len(text) == 6000
    assert "original request" in text and "middle of transcript omitted" in text
    assert text.endswith("ERROR at end\nOutput: /tmp/result.txt")


def test_freeform_summary_restores_and_can_be_compacted_incrementally(make_session):
    session = make_session(
        faux.reply("old work " * 1000),
        faux.reply("Keep the original goal; the user corrected the target to /tmp/new. Pending: verification."),
        persist=True, settings={"compaction": {"keepRecentTokens": 10}},
    )
    session.prompt("original request")
    session.compact()
    restored = make_session(
        faux.reply("new progress " * 1000), faux.reply("Target /tmp/new is ready; verification still pending."),
        session=session.store.path, settings={"compaction": {"keepRecentTokens": 10}},
    )
    assert "user corrected the target" in restored.main.messages[0].text()
    restored.prompt("continue that work")
    result = restored.compact()
    assert result is not None and "verification still pending" in result["summary"]
    request = calls()[-1]["messages"][0]["content"]
    assert "<previous-summary>" in request and "user corrected the target" in request
    assert "continue that work" in request
    assert any("new progress" in m.text() for m in restored.main.messages)
    assert any(m.text() == "continue that work" for m in restored.main.messages)


def test_incremental_compaction_rebuilds_file_lists_once(make_session):
    blocks = "<read-files>\nspec.md\n</read-files>\n\n<modified-files>\nresult.py\n</modified-files>"
    original = make_session(persist=True)
    # Resume a session written before deduplication, then simulate a summarizer emitting its own lists.
    original.store.append("compaction", summary=f"Earlier progress\n\n{blocks}\n\n{blocks}", kept_ids=[],
                          details={"read_files": ["spec.md"], "modified_files": ["result.py"]})
    session = make_session(session=original.store.path, settings={"compaction": {"keepRecentTokens": 1}})
    for i in range(2):
        faux.push("test", faux.reply("recent answer"), faux.reply(f"Earlier progress, step {i}\n\n{blocks}"))
        session.prompt(f"next step {i}")
        result = session.compact()
        previous = calls()[-1]["messages"][0]["content"].split("</previous-summary>")[0]
        assert "<previous-summary>" in previous and "Earlier progress" in previous
        assert "<read-files>" not in previous and "<modified-files>" not in previous
        expected = f"Earlier progress, step {i}\n\n{blocks}"
        assert result["summary"] == session.store.last_compaction()["summary"] == expected
        assert expected in session.main.messages[0].text()
        assert SessionStore.load(session.store.path).build_context() == (session.main.messages, session.main.ids)


def test_stripping_a_thinking_only_tail_does_not_leave_a_bare_request(make_session):
    session = make_session(faux.reply(thinking="thinking only"), faux.reply("Work in progress"),
                           persist=True, settings={"compaction": {"keepRecentTokens": 1}})
    session.prompt("Investigate the issue")
    assert session.compact()["kept"] == 0
    assert [m.kind for m in session.main.messages] == ["summary"]
    assert SessionStore.load(session.store.path).build_context() == (session.main.messages, session.main.ids)


@pytest.mark.parametrize("reason", ["auto", "overflow"])
def test_full_mid_turn_compaction_continues_after_tool_result(make_session, project, reason):
    (project / "spec.md").write_text("important facts\n" * 1200, encoding="utf-8")
    request = "Read spec.md and report its conclusion."
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 1}})
    if reason == "auto":
        session.settings.set("compaction.triggerTokens", session.context_tokens() + 500)
    faux.push("test", faux.reply(tools=[("read", {"path": "spec.md"})]))
    if reason == "overflow":
        faux.push("test", faux.error("context overflow", kind="overflow"))
    faux.push("test", faux.reply("Read spec.md successfully. The conclusion is ready to report."), faux.reply("Done"))
    session.prompt(request)

    entry = session.store.last_compaction()
    assert entry["reason"] == reason and entry["kept_ids"] == []
    sent = calls()[-1]["messages"]
    assert sent[0]["kind"] == "summary" and "conclusion is ready" in sent[0]["content"]
    assert not any(m["role"] == "user" and m["kind"] is None for m in sent)
    assert "continue" in " ".join(m["content"] for m in sent if m["role"] == "user").lower()
    assert session.main.agent.last_text == "Done"
    assert SessionStore.load(session.store.path).build_context() == (session.main.messages, session.main.ids)


def test_auto_compaction_waits_for_growth_after_nonshrinking_summary(make_session):
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 1}})
    runtime = session.main
    trigger = runtime.context_tokens()
    session.settings.set("compaction.triggerTokens", trigger)
    faux.push("test", faux.reply("small task"), faux.reply("ok"))
    session.prompt("hi")
    first = session.store.last_compaction()
    assert first["reason"] == "auto"
    assert runtime.context_tokens() >= first["tokens_before"] >= trigger
    faux.push("test", faux.reply("still here"))
    session.prompt("and now?")
    assert session.store.last_compaction() == first
    assert runtime.agent.last_text == "still here"

    # The automatic guard must still allow a provider overflow and an explicit /compact.
    faux.push("test", faux.error("context overflow", kind="overflow"), faux.reply("updated"), faux.reply("fits"))
    session.prompt("next?")
    assert session.store.last_compaction()["reason"] == "overflow" and runtime.agent.last_text == "fits"
    faux.push("test", faux.reply("manual summary"))
    assert session.compact() and session.store.last_compaction()["reason"] == "manual"

    faux.push("test", faux.reply("after growth"), faux.reply("finished"))
    session.prompt("new material " * 2000)
    assert session.store.last_compaction()["reason"] == "auto" and runtime.agent.last_text == "finished"


def test_reported_usage_after_compaction_does_not_count_as_new_content(make_session):
    session = make_session(settings={"compaction": {"keepRecentTokens": 1}})
    trigger = session.context_tokens()
    session.settings.set("compaction.triggerTokens", trigger)
    # The provider counts escaped Unicode in its request. The local estimate uses character length.
    faux.push("test", faux.reply("已读完文件。" * 300), faux.reply("ok"))
    session.prompt("read the file")
    first = session.store.last_compaction()
    faux.push("test", faux.reply("yes"))
    session.prompt("done?")
    assert session.store.last_compaction() == first and session.main.agent.last_text == "yes"

    faux.push("test", faux.reply("updated summary"), faux.reply("done"))
    session.prompt("new information " * 1000)
    second = session.store.last_compaction()
    assert second != first and second["reason"] == "auto" and session.main.agent.last_text == "done"

    # With ample headroom, the ordinary provider-reported threshold still wins.
    session.settings.set("compaction.triggerTokens", trigger + 10_000)
    reply = session.main.messages[-1]
    reply.usage = Usage(input=trigger + 9999)
    assert not session.main._compaction_due()
    reply.usage.input += 1
    assert session.main._compaction_due()
