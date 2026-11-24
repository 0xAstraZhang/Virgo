"""Summary compaction: where to cut, size estimates, and manual, automatic and reactive runs."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from virgo import ai
from virgo.agent import AgentEvent, AgentTool
from virgo.ai.providers import faux, responses
from virgo.ai.types import (
    AssistantMessage,
    ImageContent,
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

from tests.support.harness import RecordingUI, calls, install_responses, response_stream


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


def test_portable_compaction_reloads_image_in_kept_tail(make_session, pixel_png):
    session = make_session(faux.reply("earlier answer"), persist=True, settings={"compaction": {"keepRecentTokens": 1250}})
    session.prompt("earlier request")
    faux.push("test", faux.reply(tools=[("read", {"path": "pixel.png"}, "read-1")]), faux.reply("done"))
    session.prompt("Inspect the image. " * 20)
    faux.push("test", scripted())
    assert session.compact()["kept"] == 4
    restored, ids = SessionStore.load(session.store.path).build_context()
    assert (restored, ids) == (session.main.messages, session.main.ids)
    summary, user, assistant, result, _ = restored
    assert summary.kind == "summary" and user.text().startswith("Inspect the image.")
    assert assistant.tool_calls()[0].id == result.tool_call_id == "read-1"
    assert result.content == [TextContent("Image: pixel.png"), ImageContent(pixel_png, "image/png")]
    assert responses.convert_messages(restored)[-2]["output"] == [
        {"type": "input_text", "text": "Image: pixel.png"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{pixel_png}", "detail": "auto"},
    ]


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


OC_PROVIDERS = {"faux": {"models": {"test": {"contextWindow": 65_536, "maxTokens": 32_768}}}}


def length_limited(output=12_096):
    return AssistantMessage([TextContent("unfinished checkpoint")], stop_reason="length",
                            usage=Usage(input=33_209, cache_read=384, output=output))


def summary_calls(session):
    return [row for row in session.store.custom("model_call") if row["purpose"] == "compaction"]


@pytest.mark.parametrize("window,normal,recovery,reasoning,model_max", [
    pytest.param(65_536, 2048, 1024, True, 32_768, id="64k-reasoning"),
    pytest.param(65_536, 2048, 1024, False, 32_768, id="64k-plain"),
    pytest.param(1_000_000, 8000, 1024, True, 10_000, id="1m-model-cap"),
    pytest.param(256_000, 8000, 1024, False, 32_768, id="target-cap"),
    pytest.param(32_800, 1025, 1024, True, 32_768, id="above-recovery-cap"),
    pytest.param(32_768, 1024, 512, True, 32_768, id="at-recovery-cap"),
    pytest.param(16_384, 512, 256, True, 32_768, id="16k"),
    pytest.param(4096, 256, 128, False, 32_768, id="target-floor"),
    pytest.param(0, 8000, 1024, True, 32_768, id="unknown"),
    pytest.param(-1, 8000, 1024, False, 32_768, id="negative"),
])
def test_portable_prose_and_completion_budgets(make_session, monkeypatch, window, normal, recovery, reasoning, model_max):
    session = make_session(faux.reply("Earlier work"))
    session.prompt("Original request")
    # The checkpoint has to fit the active fallback window. The auxiliary model generates it.
    target = replace(session.resolve("faux/fallback"), context_window=window)
    session.small_model = replace(session.resolve("faux/small"), context_window=200_000, reasoning=reasoning,
                                  supported_reasoning_levels=("low",) if reasoning else (), max_tokens=model_max)
    allowances = []
    complete = session.complete

    def record(purpose, system, prompt, **kwargs):
        allowances.append(kwargs["max_tokens"])
        return complete(purpose, system, prompt, **kwargs)

    monkeypatch.setattr(session, "complete", record)
    seen = install_responses(monkeypatch, length_limited(), AssistantMessage([TextContent("Recovered")]))
    assert session.main.compact(reason="auto", model=target) is None
    assert session.main.compact(reason="overflow", model=target)["summary"] == "Recovered"

    for (model, context), budget in zip(seen, (normal, recovery), strict=True):
        assert model is session.small_model
        instructions = context.messages[0].text().split("</conversation>")[-1].lower()
        assert f"{budget} visible tokens" in instructions and f"~{budget * 4} characters" in instructions
        assert "earlier evicted turns" in instructions and "retained recent messages" in instructions
        assert "obsolete" in instructions and "inventories" in instructions
    assert allowances == [8000, recovery]
    ordinary, retry = summary_calls(session)
    assert ordinary["max_tokens"] == min(12_096, model_max)
    assert retry["max_tokens"] == recovery + 4096 < ordinary["max_tokens"]
    assert ordinary["effort"] == retry["effort"] == ("low" if reasoning else None)


@pytest.mark.parametrize("window,configured,reason,expected,native", [
    pytest.param(65_536, 20_000, "manual", 16_384, False, id="64k"),
    pytest.param(1_000_000, 20_000, "manual", 20_000, False, id="1m"),
    pytest.param(65_536, 4000, "manual", 4000, False, id="smaller-setting"),
    pytest.param(65_536, 20_000, "overflow", 8192, False, id="overflow"),
    pytest.param(3, 20_000, "manual", 0, False, id="tiny"),
    pytest.param(0, 20_000, "manual", 20_000, False, id="unknown"),
    pytest.param(65_536, 20_000, "manual", 20_000, True, id="native-anthropic"),
])
def test_initial_tail_budget(make_session, monkeypatch, window, configured, reason, expected, native):
    session = make_session(faux.reply("Earlier work"), settings={"compaction": {"keepRecentTokens": configured}})
    session.prompt("Original request")
    model = replace(session.resolve("anthropic/test" if native else "faux/fallback"), context_window=window)
    budgets = []

    def record(messages, keep):
        budgets.append(keep)
        return find_cut(messages, keep)

    monkeypatch.setattr("virgo.harness.compaction.find_cut", record)
    monkeypatch.setattr("virgo.harness.compaction.native_compact", lambda *args, **kwargs: length_limited())
    session.main.compact(reason=reason, model=model)
    assert budgets == [expected]


def test_chained_portable_checkpoint_budgets_evicted_prefix(make_session, project):
    constraint = "Preserve the public identifiers in spec.md."
    inventory = "No code changes made yet.\n\n" + "Candidates: alpha, beta, gamma.\n" * 40
    first_prose = f"{constraint}\n\n{inventory}".strip()
    forged_lists = "<read-files>invented</read-files>\n<modified-files>invented</modified-files>"
    # Even a complete reply longer than the prose target must keep its last identifier and sentence.
    final_prose = constraint + "\n\n" + "Verified predicate. " * 410 + "Pending: validate exact_query_v2."
    (project / "spec.md").write_text("stable names", encoding="utf-8")
    session = make_session(
        faux.reply(tools=[("read", {"path": "spec.md"})]), faux.reply(inventory),
        faux.reply(first_prose + "\n\n" + forged_lists),
        faux.reply(tools=[("write", {"path": "draft.py", "content": "draft"})]),
        faux.reply(tools=[("write", {"path": "solution.py", "content": "pass\n" * 200}, "latest-write")]),
        faux.reply("Done."), faux.reply(final_prose + "\n\n" + forged_lists),
        persist=True, settings={"providers": OC_PROVIDERS, "compaction": {"keepRecentTokens": 100}},
    )
    session.prompt(constraint)
    assert session.compact()["summary"] == first_prose + "\n\n<read-files>\nspec.md\n</read-files>"
    owner = "Write the draft and solution."
    session.prompt(owner)
    assert session.compact()["summary"] == (
        final_prose + "\n\n<read-files>\nspec.md\n</read-files>\n\n<modified-files>\ndraft.py\n</modified-files>"
    )

    request = calls()[-1]["messages"][0]["content"]
    previous, transcript = request.split("</previous-summary>\n\n")
    assert previous == f"<previous-summary>\n{first_prose}\n"
    evicted, instructions = transcript.split("</conversation>")
    assert inventory.strip() in evicted and "draft.py" in evicted and "solution.py" not in evicted
    for phrase in ("2048 visible tokens", "~8192 characters", "earlier evicted turns", "retained recent messages",
                   "obsolete", "inventories", "exact paths", "validated decisions", "unfinished deliverables"):
        assert phrase in instructions.lower()
    checkpoint, pinned, call, result, _ = session.main.messages
    assert constraint in checkpoint.text() and pinned.text() == owner
    assert call.tool_calls()[0].id == result.tool_call_id == "latest-write"
    assert not result.is_error and "solution.py" in result.text()
    assert SessionStore.load(session.store.path).build_context() == (session.main.messages, session.main.ids)


@pytest.mark.parametrize("locale,limit_label", [("en", "output token limit"), ("zh_CN", "上限")])
@pytest.mark.parametrize("native", [False, True], ids=["portable", "anthropic"])
def test_length_diagnostic_and_accounting(make_session, monkeypatch, locale, limit_label, native):
    session = make_session(faux.reply("prior work"), persist=True, settings={"language": locale, "providers": OC_PROVIDERS})
    session.prompt("Original task")
    if native:
        session.set_model("anthropic/test")
        reply = length_limited(output=7900)
        monkeypatch.setattr("virgo.harness.compaction.native_compact", lambda *a, **kw: reply)
        ceiling = 8000
    else:
        reply = length_limited()
        install_responses(monkeypatch, reply)
        ceiling = 12_096
    rt = session.main
    before = list(rt.messages), list(rt.ids)
    assert rt.compact(reason="auto") is None
    assert before == (rt.messages, rt.ids) == SessionStore.load(session.store.path).build_context()
    assert session.store.last_compaction() is None
    row, = summary_calls(session)
    assert row["stop_reason"] == "length"
    assert row["usage"] == dict(input=33_209, cache_read=384, output=reply.usage.output, cache_write=0)
    diagnostic = session.ui.lines[-1].lower().replace(",", "")
    assert "length" in diagnostic and limit_label in diagnostic and str(ceiling) in diagnostic
    assert session.t("harness.compact.empty_summary") not in diagnostic


@pytest.mark.parametrize("native", [False, True], ids=["portable", "anthropic"])
@pytest.mark.parametrize("stop,text,error,expected", [
    ("stop", "  ", None, "empty summary"),
    ("error", "", "provider unavailable", "provider unavailable"),
    ("stop", "partial", "provider unavailable", "provider unavailable"),
    ("aborted", "partial", None, "cancel"),
])
def test_compaction_distinguishes_other_failures(make_session, monkeypatch, native, stop, text, error, expected):
    session = make_session(faux.reply("prior work"))
    session.prompt("Original task")
    reply = AssistantMessage([TextContent(text)], stop_reason=stop, error_message=error)
    if native:
        session.set_model("anthropic/test")
        monkeypatch.setattr("virgo.harness.compaction.native_compact", lambda *a, **kw: reply)
    else:
        install_responses(monkeypatch, reply)
    assert session.compact() is None
    assert expected in session.ui.lines[-1].lower()
    assert session.store.last_compaction() is None
    assert len(summary_calls(session)) == 1


@pytest.mark.parametrize("before,main_input,after,at_reserve", [
    pytest.param(57_196, 59_956, 65_474, True, id="OC-F1"),
    pytest.param(63_763, 63_569, 63_923, True, id="OC-F2"),
    pytest.param(60_665, 61_273, 63_439, True, id="OC-F3"),
    pytest.param(57_196, 59_956, 61_439, False, id="one-before-reserve"),
    pytest.param(57_196, 59_956, 61_440, True, id="at-reserve"),
])
def test_length_then_tool_turn_bounds_recovery(make_session, monkeypatch, project, before, main_input, after, at_reserve):
    session = make_session(persist=True, settings={"providers": OC_PROVIDERS})
    rt = session.main
    (project / "spec").write_text("fact", encoding="utf-8")
    install_responses(monkeypatch, AssistantMessage([TextContent("prior work")], usage=Usage(input=before - 1)))
    session.prompt("Original task")
    original = list(rt.messages)
    # "Read" and the real tool result "1|fact" each add one token to the preceding reported usage.
    tool_turn = AssistantMessage([ToolCall("read-spec", "read", {"path": "spec"})], stop_reason="tool_use",
                                usage=Usage(input=main_input, output=after - main_input - 1))
    overflow = AssistantMessage(stop_reason="error", error_kind="overflow", error_message="context is too long")
    replies = iter([tool_turn, overflow])
    requests, instructions, model_contexts = [], [], []

    def stream(model, ctx, opts):
        if "context summarization assistant" in ctx.system:
            requests.append("summary")
            instructions.append(ctx.messages[0].text().split("</conversation>")[-1])
            if len(instructions) == 2 and after == 61_440:
                return response_stream(AssistantMessage())  # An empty recovery also exhausts the attempt.
            return response_stream(length_limited(output=opts.max_tokens))
        requests.append("main")
        model_contexts.append(list(ctx.messages))
        return response_stream(next(replies))

    monkeypatch.setattr(ai, "stream", stream)
    session.prompt("Read")
    assert requests == (["summary", "main", "summary", "main"] if at_reserve
                        else ["summary", "main", "main", "summary"])
    ordinary, recovery = summary_calls(session)
    assert ordinary["max_tokens"] == 12_096 and recovery["max_tokens"] < ordinary["max_tokens"]
    assert instructions[0] != instructions[1]
    assert re.search(r"\b\d[\d,]* (?:visible )?tokens\b", instructions[1])
    assert model_contexts[0][:-1] == original
    assert model_contexts[1][:-2] == model_contexts[0]
    call, result = model_contexts[1][-2:]
    assert call.tool_calls()[0].id == result.tool_call_id == "read-spec" and result.text() == "1|fact"
    assert rt.messages == [*model_contexts[1], overflow]
    restored, ids = SessionStore.load(session.store.path).build_context()
    assert ids == rt.ids and restored[:-2] == rt.messages[:-2] and restored[-1] == overflow
    # The store compacts tool policy metadata on disk. All tool message fields sent to the model survive.
    assert replace(restored[-2], details=None) == replace(result, details=None)
    assert session.store.last_compaction() is None


@pytest.mark.parametrize("reactive", [False, True], ids=["auto", "overflow"])
@pytest.mark.parametrize("cancellation", ["signal-and-length", "aborted"])
def test_cancelled_compaction_ends_run_without_retry(make_session, monkeypatch, cancellation, reactive):
    session = make_session(settings={"compaction": {"triggerTokens": 1}})
    rt = session.main
    requests = []

    def stream(model, ctx, opts):
        if "context summarization assistant" in ctx.system:
            requests.append("summary")
            if reactive and requests.count("summary") == 1:
                return response_stream(length_limited())
            if cancellation == "signal-and-length":
                opts.signal.set()
                return response_stream(length_limited())
            return response_stream(AssistantMessage(stop_reason="aborted"))
        requests.append("main")
        return response_stream(AssistantMessage(stop_reason="error", error_kind="overflow", error_message="too long"))

    monkeypatch.setattr(ai, "stream", stream)
    session.prompt("Original task")
    assert requests == (["summary", "main", "summary"] if reactive else ["summary"])
    assert rt.messages[-1].stop_reason == "aborted"
    assert "cancel" in session.ui.lines[-1].lower()
    assert session.store.last_compaction() is None
    if not reactive:
        assert rt._compaction_due()  # Even a racing length response did not arm the length guard.


@pytest.fixture
def after_length(make_session, monkeypatch):
    session = make_session(settings={"providers": OC_PROVIDERS})
    install_responses(monkeypatch, AssistantMessage([TextContent("work")], usage=Usage(input=57_196)), length_limited())
    session.prompt("Original task")
    assert session.main.compact(reason="auto") is None
    return session


@pytest.mark.parametrize("reason", ["manual", "auto", "overflow"])
def test_compaction_success_clears_length_failure(after_length, monkeypatch, reason):
    session = after_length
    rt = session.main
    assert not rt._compaction_due()
    rt.messages[-1].usage.input = 61_440
    assert rt._compaction_due()
    install_responses(monkeypatch, AssistantMessage([TextContent("complete checkpoint")]))
    assert rt.compact(reason=reason)["summary"] == "complete checkpoint"
    # Growth in this same run must allow ordinary compaction again, below the safety reserve.
    rt.messages.append(AssistantMessage(usage=Usage(input=57_196)))
    assert rt._compaction_due()
    install_responses(monkeypatch, length_limited())
    rt.compact(reason="auto")
    assert [c["max_tokens"] for c in summary_calls(session)][-1] == 12_096


@pytest.mark.parametrize("reset", ["set_model", "window_only", "restore_native_context", "prepare_run", "new_prompt"])
def test_length_failure_resets_at_runtime_boundaries(after_length, monkeypatch, reset):
    session = after_length
    rt = session.main
    assert not rt._compaction_due()
    if reset == "set_model":
        session.set_model("faux/other")
        session.set_model("faux/test")
    elif reset == "window_only":
        original = rt.model
        rt.model = replace(original, context_window=original.context_window - 1)
        rt.model = original
    elif reset == "restore_native_context":
        rt.restore_native_context(session.resolve("faux/other"))
    elif reset == "prepare_run":
        rt.prepare_run()
    else:
        install_responses(monkeypatch, length_limited(), AssistantMessage([TextContent("done")]))
        session.prompt("Continue")
        assert session.main.agent.last_text == "done"
        assert [row["max_tokens"] for row in summary_calls(session)] == [12_096, 12_096]
        return
    assert rt._compaction_due()


def test_length_failure_resets_on_retry_fallback(make_session, monkeypatch):
    session = make_session(settings={
        "providers": OC_PROVIDERS,
        "compaction": {"triggerTokens": 1},
        "retry": {"fallbackModel": "faux/fallback"},
    })
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)
    overloaded = AssistantMessage(stop_reason="error", error_kind="retryable", error_message="overloaded")
    seen = install_responses(monkeypatch, length_limited(), overloaded, overloaded,
                             AssistantMessage([TextContent("checkpoint")]), AssistantMessage([TextContent("done")]))
    session.prompt("Original task")
    assert session.main.agent.last_text == "done" and seen[-1][0].spec == "faux/fallback"
    assert [row["max_tokens"] for row in summary_calls(session)] == [12_096, 12_096]


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_native_length_recovery_requires_shorter_request(make_session, monkeypatch, api):
    session = make_session(faux.reply("prior work"))
    session.prompt("Original task")
    session.set_model(f"{api}/test")
    rt = session.main
    requests = []

    def native(model, context, opts, *, focus):
        requests.append((opts.max_tokens, focus))
        return length_limited(output=opts.max_tokens)

    monkeypatch.setattr("virgo.harness.compaction.native_compact", native)
    assert rt.compact(reason="auto") is None
    assert rt.compact(reason="overflow") is None
    if api == "anthropic":
        ordinary, recovery = requests
        assert recovery[0] < ordinary[0] and recovery[1] and "tokens" in recovery[1]
    else:
        assert len(requests) == 1  # The standalone endpoint accepts neither focus nor an output limit.


def test_successful_length_recovery_keeps_one_reactive_overflow_guard(make_session, monkeypatch):
    session = make_session(settings={"compaction": {"triggerTokens": 1}})
    overflow = AssistantMessage(stop_reason="error", error_kind="overflow", error_message="context is too long")
    install_responses(monkeypatch, length_limited(), overflow, AssistantMessage([TextContent("checkpoint")]), overflow)
    session.prompt("Original task")
    assert session.main.messages[-1].error_message == "context is too long"
    assert session.store.last_compaction()["reason"] == "overflow"
    ordinary, recovery = summary_calls(session)
    assert recovery["max_tokens"] < ordinary["max_tokens"]
