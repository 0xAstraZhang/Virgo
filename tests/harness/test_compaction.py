"""Three-layer context compaction: L1 micro-compaction, L2 structured summary, and L3 overflow recovery."""

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
from virgo.harness.compaction import (
    PRUNE_PREFIX,
    estimate_tokens,
    find_cut,
    prune_tool_results,
)
from virgo.harness.session import AgentSession
from virgo.harness.store import SessionStore

from tests.support.harness import RecordingUI, calls, install_responses, response_stream


def _is_summary_request(ctx) -> bool:
    if "context summarization assistant" in ctx.system:
        return True
    if ctx.messages and isinstance(ctx.messages[-1], UserMessage):
        return ctx.messages[-1].kind == "compaction_prompt"
    return False


def _is_summary_call(call: dict) -> bool:
    if "context summarization assistant" in call["system"]:
        return True
    msgs = call.get("messages") or []
    return bool(msgs and msgs[-1].get("kind") == "compaction_prompt")


def summarizer(ctx):
    if _is_summary_request(ctx):
        return faux.reply(
            "## Goal & Constraints\nbuild the thing\n"
            "## Completed Work & Decisions\nnone\n"
            "## Current State & Open Issues\nin progress\n"
            "## Next Steps\n1. finish"
        )
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


def test_estimate_tokens_weights_cjk_higher_than_ascii():
    ascii_est = estimate_tokens([UserMessage("a" * 300)])
    cjk_est = estimate_tokens([UserMessage("中" * 300)])
    assert ascii_est == 75
    assert cjk_est == 200
    assert cjk_est > ascii_est * 2


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
    assert res is not None and res["tokens_before"] > 0 and res["strategy"] == "in_context"
    msgs = s.main.messages
    assert msgs[0].kind == "summary" and "build the thing" in msgs[0].text()
    assert not any(
        isinstance(b, ThinkingContent) for m in msgs if isinstance(m, AssistantMessage) for b in m.content
    )
    assert any(
        isinstance(m, UserMessage) and m.text() == "question 2" for m in msgs
    )  # current request pinned/kept
    # In-context summarizer saw the prefix messages and the focus instruction at the end.
    summary_req = [c for c in calls() if _is_summary_call(c)][0]
    assert "the API" in summary_req["messages"][-1]["content"]
    assert any("question 0" in str(m.get("content", "")) for m in summary_req["messages"][:-1])
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


@pytest.mark.parametrize("trigger, budget, expected_requests", [
    pytest.param(1_000_000, 2, ["task", "task"], id="deferred-summary"),
    pytest.param(1, 1, ["task"], id="no-budget-for-summary"),
    pytest.param(1, 2, ["task", "summary"], id="summary-spends-last-call"),
    pytest.param(1, 3, ["task", "summary", "task"], id="summary-and-task-fit"),
])
def test_auto_compaction_and_task_share_request_budget(make_session, trigger, budget, expected_requests):
    session = make_session(
        faux.reply("Prior findings. " * 300),
        persist=True,
        refresh_models=False,
        settings={
            "budget": {"maxModelCalls": budget},
            "compaction": {"triggerTokens": 1_000_000, "keepRecentTokens": 50, "pruneRatio": 0.0},
        },
    )
    session.prompt("Inspect the task before continuing.")
    session.settings.set("compaction.triggerTokens", trigger)
    faux.handler("test", scripted(faux.reply("Task finished.")))

    session.prompt("Continue the task.")

    assert ["summary" if _is_summary_call(call) else "task" for call in calls()] == expected_requests
    checkpoint = SessionStore.load(session.store.path).last_compaction(mode="summary")
    assert (checkpoint is not None) == ("summary" in expected_requests)
    if expected_requests[-1] == "task" and len(expected_requests) > 1:
        assert session.main.agent.last_text == "Task finished."
    else:
        failure = session.main.messages[-1]
        assert failure.stop_reason == "error"
        assert "budget" in failure.error_message.lower()
    assert len(calls()) <= budget


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


# ---------------------------------------------------------------------------
# L1: Batch Micro-Compaction tests
# ---------------------------------------------------------------------------


def test_l1_micro_compaction_prunes_old_tool_results_and_skips_l2(make_session, project):
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (project / name).write_text((f"{name} line\n") * 400, encoding="utf-8")
    session = make_session(
        faux.reply(tools=[("read", {"path": "a.txt"}, "r1")]),
        faux.reply(tools=[("read", {"path": "b.txt"}, "r2")]),
        faux.reply(tools=[("read", {"path": "c.txt"}, "r3")]),
        faux.reply(tools=[("read", {"path": "d.txt"}, "r4")]),
        faux.reply("all read"),
        persist=True,
        settings={
            "providers": {"faux": {"models": {"test": {"contextWindow": 10_000}}}},
            "compaction": {"pruneRatio": 0.6, "triggerRatio": 0.8, "keepRecentTokens": 1500},
        },
    )
    session.prompt("Read four files.")
    assert session.main.agent.last_text == "all read"
    # L1 pruned older tool results (a.txt / b.txt) without triggering an L2 LLM summary call.
    assert summary_calls(session) == []
    assert session.store.last_compaction(mode="summary") is None
    prune_entry = session.store.last_compaction(mode="prune")
    assert prune_entry is not None and prune_entry["pruned"]
    pruned_texts = [m.text() for m in session.main.messages if isinstance(m, ToolResultMessage) and m.text().startswith(PRUNE_PREFIX)]
    assert pruned_texts and any("a.txt" in t and "Re-read with read()" in t for t in pruned_texts)
    # Reloading from disk matches the pruned live context (tool policy metadata is compacted on disk).
    loaded_msgs, loaded_ids = SessionStore.load(session.store.path).build_context()
    assert loaded_ids == session.main.ids
    assert [m.text() for m in loaded_msgs] == [m.text() for m in session.main.messages]


def test_l1_micro_compaction_skips_tiny_reclaim_to_preserve_cache(make_session, project):
    for name in ("a.txt", "b.txt", "c.txt"):
        (project / name).write_text("short content\n" * 20, encoding="utf-8")
    session = make_session(
        faux.reply(tools=[("read", {"path": "a.txt"}, "r1")]),
        faux.reply(tools=[("read", {"path": "b.txt"}, "r2")]),
        faux.reply(tools=[("read", {"path": "c.txt"}, "r3")]),
        faux.reply("done"),
        settings={"compaction": {"keepRecentTokens": 50}},
    )
    session.prompt("Read three short files.")
    assert prune_tool_results(session.main, model=session.model, reason="auto") is None


# ---------------------------------------------------------------------------
# L2: Cache-Warm In-Context Summarization + Fallback tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_reply", [
    pytest.param(AssistantMessage([ToolCall("t1", "read", {"path": "x"})], stop_reason="tool_use"), id="tool_use"),
    pytest.param(AssistantMessage(stop_reason="error", error_kind="overflow", error_message="too long"), id="overflow"),
])
def test_l2_in_context_falls_back_to_serialized_on_tool_use_or_overflow(make_session, monkeypatch, failure_reply):
    session = make_session(faux.reply("earlier work"))
    session.prompt("Build the parser")
    seen = install_responses(
        monkeypatch,
        failure_reply,
        AssistantMessage([TextContent("## Goal & Constraints\nBuild parser\n## Next Steps\nTest it")]),
    )
    res = session.compact(focus="parser rules")
    assert res is not None and res["strategy"] == "serialized"
    assert "Build parser" in res["summary"]
    assert len(seen) == 2
    # First attempt was in-context on the main model with the conversation prefix + compaction_prompt.
    in_ctx_model, in_ctx = seen[0]
    assert in_ctx_model is session.model
    assert in_ctx.messages[-1].kind == "compaction_prompt"
    assert "## Goal & Constraints" in in_ctx.messages[-1].text()
    assert "parser rules" in in_ctx.messages[-1].text()
    # Second attempt fell back to out-of-band serialized summary on small_model.
    ser_model, ser_ctx = seen[1]
    assert ser_model is session.small_model
    assert "context summarization assistant" in ser_ctx.system
    assert "<conversation>" in ser_ctx.messages[0].text()


def test_compaction_requests_exact_formats_on_both_summary_paths(make_session, monkeypatch):
    requirement = "Write exactly `A\t0042` followed by LF."
    session = make_session(faux.reply("Acknowledged."))
    session.prompt(requirement)
    requests = install_responses(
        monkeypatch,
        AssistantMessage(stop_reason="error", error_kind="overflow", error_message="too long"),
        AssistantMessage([TextContent(requirement)]),
    )
    result = session.compact()
    assert result is not None and result["strategy"] == "serialized"
    assert requirement in result["summary"]
    assert len(requests) == 2
    for _, context in requests:
        prompt = "\n".join(message.text() for message in context.messages)
        assert requirement in prompt
        assert "Quote exact output-format requirements" in prompt
        assert "User example (verbatim):" in prompt
        assert "If a correction changes any part of an example, drop the example and retain the stated correction." in prompt
        assert "Record only open issues raised or evidenced in the transcript and actions that remain pending." in prompt
        assert "Do not draft output, calculate sample values, or speculate about unstated format cases." in prompt


def test_chained_compaction_distinguishes_previous_notes_from_user_format_examples(make_session, monkeypatch):
    original = "Use prefix `TA=`. User example for input 7 is `TA=0007`."
    previous = "## Goal & Constraints\nUser example (verbatim): User example for input 7 is `TA=0007`."
    correction = "Change the prefix to `ta=` and retain the other requirements."
    session = make_session(
        faux.reply("Acknowledged."), faux.reply("Waiting."), faux.reply(previous), faux.reply("Acknowledged."),
        settings={"compaction": {"keepRecentTokens": 0}},
    )
    session.prompt(original)
    session.prompt("Wait for dispatch.")
    assert session.compact() is not None
    session.prompt(correction)
    requests = install_responses(
        monkeypatch,
        AssistantMessage(stop_reason="error", error_kind="overflow", error_message="too long"),
        AssistantMessage([TextContent(correction)]),
    )
    result = session.compact()
    assert result is not None and result["strategy"] == "serialized"
    assert len(requests) == 2
    for _, context in requests:
        prompt = "\n".join(message.text() for message in context.messages)
        assert previous in prompt and correction in prompt and original not in prompt
        assert "Treat the earlier summary as model-generated notes." in prompt
        assert "Carry forward an example from it only if explicitly attributed to the user and still applicable verbatim." in prompt


# ---------------------------------------------------------------------------
# L3: Reactive Overflow Recovery tests
# ---------------------------------------------------------------------------


def test_l3_overflow_recovery_prunes_before_summarizing(make_session, project):
    for name in ("a.txt", "b.txt", "c.txt"):
        (project / name).write_text((f"{name} payload\n") * 500, encoding="utf-8")
    session = make_session(
        faux.reply(tools=[("read", {"path": "a.txt"}, "r1")]),
        faux.reply(tools=[("read", {"path": "b.txt"}, "r2")]),
        faux.reply(tools=[("read", {"path": "c.txt"}, "r3")]),
        faux.error("prompt is too long", kind="overflow"),
        faux.reply("recovered after prune"),
        persist=True,
        settings={"compaction": {"pruneRatio": 0.0, "keepRecentTokens": 50}},
    )
    session.prompt("Read three files.")
    assert session.main.agent.last_text == "recovered after prune"
    # Aggressive L1 prune resolved the overflow without calling L2 summarization.
    assert summary_calls(session) == []
    assert session.store.last_compaction(mode="prune")["reason"] == "overflow"
    assert session.store.last_compaction(mode="summary") is None


def test_l3_overflow_falls_back_to_serialized_summary_when_prune_retry_still_overflows(make_session, project):
    for name in ("a.txt", "b.txt", "c.txt"):
        (project / name).write_text((f"{name} payload\n") * 500, encoding="utf-8")
    session = make_session(
        faux.reply(tools=[("read", {"path": "a.txt"}, "r1")]),
        faux.reply(tools=[("read", {"path": "b.txt"}, "r2")]),
        faux.reply(tools=[("read", {"path": "c.txt"}, "r3")]),
        faux.error("prompt is too long", kind="overflow"),
        # Prune-only retry still overflows on token-dense input -> must fall back to L2 serialized summary.
        faux.error("still too long after prune", kind="overflow"),
        scripted(),
        faux.reply("recovered after serialized summary"),
        persist=True,
        settings={"compaction": {"pruneRatio": 0.0, "keepRecentTokens": 50}},
    )
    session.prompt("Read three files.")
    assert session.main.agent.last_text == "recovered after serialized summary"
    assert session.store.last_compaction(mode="prune")["reason"] == "overflow"
    summary = session.store.last_compaction(mode="summary")
    assert summary is not None and summary["reason"] == "overflow" and summary["strategy"] == "serialized"


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
    assert s.store.last_compaction()["strategy"] == "serialized"


# ---------------------------------------------------------------------------
# CompactionController state machine & budget tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("usage", [
    pytest.param(Usage(input=4800, output=150), id="below-trigger"),
    pytest.param(Usage(input=800, cache_read=4000, cache_write=400, output=150), id="cached-above-trigger"),
])
def test_auto_compaction_waits_for_growth_from_first_reported_input(make_session, project, monkeypatch, usage):
    previous = make_session(faux.reply("started"), faux.reply("previous progress " * 1600), persist=True)
    previous.prompt("Start the task.")
    assert previous.main.compact(reason="auto")
    previous.shutdown()
    session = make_session(
        session="continue",
        settings={"compaction": {"triggerTokens": 5000, "pruneRatio": 0.0, "keepRecentTokens": 250}},
    )
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
            decisions.append(rt.compactor.compaction_due())

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

    assert runtime.compactor._auto_floor == floor
    assert not runtime.compactor.compaction_due()
    usage = runtime.messages[-1].usage
    usage.output = due_at - baseline - 1
    assert not runtime.compactor.compaction_due()
    usage.output += 1
    assert runtime.compactor.compaction_due()
    usage.output = window + 1 - baseline
    assert runtime.compactor.compaction_due()


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

    faux.push("fallback", faux.reply("summary"))
    assert runtime.compact(reason="auto", model=target)
    assert runtime.compactor._auto_floor == 14_400
    assert runtime.compactor.compaction_due(target) == (tokens >= due_at)
    tokens = due_at - 1
    assert not runtime.compactor.compaction_due(target)
    tokens = due_at
    assert runtime.compactor.compaction_due(target)


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
    assert runtime.compactor._auto_floor == 32_000

    if switch == "set_model":
        session.set_model("faux/small")
        assert runtime.compactor.compaction_due()
    elif switch == "window_only":
        runtime.model = replace(runtime.model, context_window=16_000)
        assert runtime.compactor.compaction_due()
    else:
        assert runtime.compactor.compaction_due(session.resolve("faux/small"))


@pytest.mark.parametrize("window, expected_floor", [(200_000, 32_000), (16_000, 14_400)])
def test_post_compaction_usage_belongs_to_target_model(make_session, window, expected_floor):
    session = make_session(faux.reply("started"))
    session.prompt("Start the task.")
    runtime = session.main
    fallback = replace(session.resolve("faux/fallback"), context_window=window)
    faux.push("fallback", faux.reply("summary"))
    assert runtime.compact(reason="auto", model=fallback)
    estimate_floor = runtime.compactor._auto_floor

    def report(model):
        reply = AssistantMessage(provider=model.provider, model=model.id, usage=Usage(input=12_000))
        runtime._on_event(AgentEvent("message_end", "main", message=reply))

    report(runtime.model)
    assert runtime.compactor._auto_floor == estimate_floor
    report(fallback)
    assert runtime.compactor._auto_floor == expected_floor
    session.settings.set("compaction.triggerTokens", 1)
    assert not runtime.compactor.compaction_due(fallback)
    assert runtime.compactor.compaction_due()  # Switching back to the primary model clears the floor.


def test_retry_compacts_portable_history_before_smaller_model_call(make_session, monkeypatch):
    session = make_session(
        faux.reply("started"), faux.reply("initial summary"),
        faux.error("overloaded", kind="retryable"), faux.error("overloaded", kind="retryable"),
        settings={
            "providers": {"faux": {"models": {"small": {"contextWindow": 16_000}}}},
            "retry": {"fallbackModel": "faux/small"},
        },
    )
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)
    session.prompt("Start the task.")
    assert session.main.compact(reason="auto")
    faux.push("small", faux.reply("fallback summary"), faux.reply("done"))
    session.prompt("x" * 68_000)
    assert session.main.agent.last_text == "done"
    assert "fallback summary" in calls("small")[-1]["messages"][0]["content"]


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
    sent = calls()[-1]["messages"]
    assert "user corrected the target" in sent[0]["content"]
    assert any("continue that work" in str(m.get("content", "")) for m in sent[:-1])
    assert any("new progress" in m.text() for m in restored.main.messages)
    assert any(m.text() == "continue that work" for m in restored.main.messages)


def test_incremental_compaction_rebuilds_file_lists_once(make_session):
    blocks = "<read-files>\nspec.md\n</read-files>\n\n<modified-files>\nresult.py\n</modified-files>"
    original = make_session(persist=True)
    original.store.append("compaction", summary=f"Earlier progress\n\n{blocks}\n\n{blocks}", kept_ids=[],
                          details={"read_files": ["spec.md"], "modified_files": ["result.py"]})
    session = make_session(session=original.store.path, settings={"compaction": {"keepRecentTokens": 1}})
    for i in range(2):
        faux.push("test", faux.reply("recent answer"), faux.reply(f"Earlier progress, step {i}\n\n{blocks}"))
        session.prompt(f"next step {i}")
        result = session.compact()
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
    settings = {"compaction": {"keepRecentTokens": 1, "pruneRatio": 0.0}}
    if reason == "auto":
        # When even a single unread tool result already exceeds the context window,
        # proactive auto-compaction must still evict it instead of letting the request overflow.
        settings["providers"] = {"faux": {"models": {"test": {"contextWindow": 2_000}}}}
    session = make_session(persist=True, settings=settings)
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
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 1, "pruneRatio": 0.0}})
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
    session = make_session(settings={"compaction": {"keepRecentTokens": 1, "pruneRatio": 0.0}})
    trigger = session.context_tokens()
    session.settings.set("compaction.triggerTokens", trigger)
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
    assert not session.main.compactor.compaction_due()
    reply.usage.input += 1
    assert session.main.compactor.compaction_due()


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
    pytest.param(4096, 256, 256, False, 32_768, id="target-floor"),
    pytest.param(0, 8000, 1024, True, 32_768, id="unknown"),
    pytest.param(-1, 8000, 1024, False, 32_768, id="negative"),
])
def test_prose_and_completion_budgets(make_session, monkeypatch, window, normal, recovery, reasoning, model_max):
    session = make_session(faux.reply("Earlier work"))
    session.prompt("Original request. " * 20_000)
    target = replace(session.resolve("faux/fallback"), context_window=window, max_tokens=model_max, reasoning=reasoning)
    session.small_model = replace(session.resolve("faux/small"), context_window=200_000, reasoning=reasoning,
                                  supported_reasoning_levels=("low",) if reasoning else (), max_tokens=model_max)
    seen = install_responses(monkeypatch, length_limited(), AssistantMessage([TextContent("Recovered")]))
    assert session.main.compact(reason="auto", model=target) is None
    assert session.main.compact(reason="overflow", model=target)["summary"] == "Recovered"

    # First call (auto) ran in-context on target; second call (overflow) ran serialized on small_model.
    (m1, ctx1), (m2, ctx2) = seen
    assert m1 is target
    assert m2 is session.small_model
    for instructions, budget in (
        (ctx1.messages[-1].text().lower(), normal),
        (ctx2.messages[0].text().split("</conversation>")[-1].lower(), recovery),
    ):
        assert f"{budget} visible tokens" in instructions and f"~{budget * 4} characters" in instructions
        assert "## goal & constraints" in instructions and "## next steps" in instructions
        assert "earlier evicted turns" in instructions and "retained recent messages" in instructions
    ordinary, retry = summary_calls(session)
    assert ordinary["max_tokens"] == min(4096 + min(8000, max(1024, normal * 2)), model_max)
    assert retry["max_tokens"] == recovery + 4096 < ordinary["max_tokens"]


@pytest.mark.parametrize("window,configured,reason,expected", [
    pytest.param(65_536, 20_000, "manual", 16_384, id="64k"),
    pytest.param(1_000_000, 20_000, "manual", 20_000, id="1m"),
    pytest.param(65_536, 4000, "manual", 4000, id="smaller-setting"),
    pytest.param(65_536, 20_000, "overflow", 8192, id="overflow"),
    pytest.param(3, 20_000, "manual", 0, id="tiny"),
    pytest.param(0, 20_000, "manual", 20_000, id="unknown"),
])
def test_initial_tail_budget(make_session, monkeypatch, window, configured, reason, expected):
    session = make_session(faux.reply("Earlier work"), settings={"compaction": {"keepRecentTokens": configured}})
    session.prompt("Original request")
    model = replace(session.resolve("faux/fallback"), context_window=window)
    budgets = []

    def record(messages, keep):
        budgets.append(keep)
        return find_cut(messages, keep)

    monkeypatch.setattr("virgo.harness.compaction.find_cut", record)
    install_responses(monkeypatch, length_limited())
    session.main.compact(reason=reason, model=model)
    assert budgets == [expected]


def test_chained_checkpoint_budgets_evicted_prefix(make_session, project):
    constraint = "Preserve the public identifiers in spec.md."
    inventory = "No code changes made yet.\n\n" + "Candidates: alpha, beta, gamma.\n" * 40
    first_prose = f"{constraint}\n\n{inventory}".strip()
    forged_lists = "<read-files>invented</read-files>\n<modified-files>invented</modified-files>"
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

    sent = calls()[-1]["messages"]
    instructions = sent[-1]["content"].lower()
    for phrase in ("256 visible tokens", "~1024 characters", "earlier evicted turns", "retained recent messages",
                   "rewrite and merge the earlier summary", "drop superseded status",
                   "## goal & constraints", "## next steps", "exact file paths", "validated decisions"):
        assert phrase in instructions
    checkpoint, pinned, call, result, _ = session.main.messages
    assert constraint in checkpoint.text() and pinned.text() == owner
    assert call.tool_calls()[0].id == result.tool_call_id == "latest-write"
    assert not result.is_error and "solution.py" in result.text()
    assert SessionStore.load(session.store.path).build_context() == (session.main.messages, session.main.ids)


@pytest.mark.parametrize("locale,limit_label", [("en", "output token limit"), ("zh_CN", "上限")])
def test_length_diagnostic_and_accounting(make_session, monkeypatch, locale, limit_label):
    session = make_session(faux.reply("prior work"), persist=True, settings={"language": locale, "providers": OC_PROVIDERS})
    session.prompt("Original task")
    reply = length_limited()
    install_responses(monkeypatch, reply)
    rt = session.main
    before = list(rt.messages), list(rt.ids)
    assert rt.compact(reason="auto") is None
    assert before == (rt.messages, rt.ids) == SessionStore.load(session.store.path).build_context()
    assert session.store.last_compaction() is None
    row, = summary_calls(session)
    assert row["stop_reason"] == "length"
    assert row["usage"] == dict(input=33_209, cache_read=384, output=reply.usage.output, cache_write=0)
    diagnostic = session.ui.lines[-1].lower().replace(",", "")
    assert "length" in diagnostic and limit_label in diagnostic and str(row["max_tokens"]) in diagnostic
    assert session.t("harness.compact.empty_summary") not in diagnostic


@pytest.mark.parametrize("stop,text,error,expected", [
    ("stop", "  ", None, "empty summary"),
    ("error", "", "provider unavailable", "provider unavailable"),
    ("stop", "partial", "provider unavailable", "provider unavailable"),
    ("aborted", "partial", None, "cancel"),
])
def test_compaction_distinguishes_other_failures(make_session, monkeypatch, stop, text, error, expected):
    session = make_session(faux.reply("prior work"))
    session.prompt("Original task")
    reply = AssistantMessage([TextContent(text)], stop_reason=stop, error_message=error)
    install_responses(monkeypatch, reply)
    assert session.compact() is None
    assert expected in session.ui.lines[-1].lower()
    assert session.store.last_compaction() is None
    assert len(summary_calls(session)) == 1


@pytest.mark.parametrize("before,main_input,after,at_reserve", [
    pytest.param(57_196, 59_956, 65_474, True, id="overflow-boundary-1"),
    pytest.param(63_763, 63_569, 63_923, True, id="overflow-boundary-2"),
    pytest.param(60_665, 61_273, 63_439, True, id="overflow-boundary-3"),
    pytest.param(57_196, 59_956, 61_439, False, id="one-before-reserve"),
    pytest.param(57_196, 59_956, 61_440, True, id="at-reserve"),
])
def test_length_then_tool_turn_bounds_recovery(make_session, monkeypatch, project, before, main_input, after, at_reserve):
    session = make_session(persist=True, settings={"providers": OC_PROVIDERS, "compaction": {"pruneRatio": 0.0}})
    rt = session.main
    (project / "spec").write_text("fact", encoding="utf-8")
    install_responses(monkeypatch, AssistantMessage([TextContent("prior work")], usage=Usage(input=before - 1)))
    session.prompt("Original task")
    original = list(rt.messages)
    tool_turn = AssistantMessage([ToolCall("read-spec", "read", {"path": "spec"})], stop_reason="tool_use",
                                usage=Usage(input=main_input, output=after - main_input - 1))
    overflow = AssistantMessage(stop_reason="error", error_kind="overflow", error_message="context is too long")
    replies = iter([tool_turn, overflow])
    requests, instructions, model_contexts = [], [], []

    def stream(model, ctx, opts):
        if _is_summary_request(ctx):
            requests.append("summary")
            prompt_text = ctx.messages[-1].text() if ctx.messages[-1].kind == "compaction_prompt" else ctx.messages[0].text().split("</conversation>")[-1]
            instructions.append(prompt_text)
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
    assert ordinary["max_tokens"] == 5120 and recovery["max_tokens"] < ordinary["max_tokens"]
    assert instructions[0] != instructions[1]
    assert re.search(r"\b\d[\d,]* (?:visible )?tokens\b", instructions[1])
    assert model_contexts[0][:-1] == original
    assert model_contexts[1][:-2] == model_contexts[0]
    call, result = model_contexts[1][-2:]
    assert call.tool_calls()[0].id == result.tool_call_id == "read-spec" and result.text() == "1|fact"
    assert rt.messages == [*model_contexts[1], overflow]
    restored, ids = SessionStore.load(session.store.path).build_context()
    assert ids == rt.ids and restored[:-2] == rt.messages[:-2] and restored[-1] == overflow
    assert replace(restored[-2], details=None) == replace(result, details=None)
    assert session.store.last_compaction() is None


@pytest.mark.parametrize("reactive", [False, True], ids=["auto", "overflow"])
@pytest.mark.parametrize("cancellation", ["signal-and-length", "aborted"])
def test_cancelled_compaction_ends_run_without_retry(make_session, monkeypatch, cancellation, reactive):
    session = make_session(settings={"compaction": {"triggerTokens": 1, "pruneRatio": 0.0}})
    rt = session.main
    requests = []

    def stream(model, ctx, opts):
        if _is_summary_request(ctx):
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
        assert rt.compactor.compaction_due()  # Even a racing length response did not arm the length guard.


@pytest.fixture
def after_length(make_session, monkeypatch):
    session = make_session(settings={"providers": OC_PROVIDERS, "compaction": {"pruneRatio": 0.0}})
    install_responses(monkeypatch, AssistantMessage([TextContent("work")], usage=Usage(input=57_196)), length_limited())
    session.prompt("Original task")
    assert session.main.compact(reason="auto") is None
    return session


@pytest.mark.parametrize("reason", ["manual", "auto", "overflow"])
def test_compaction_success_clears_length_failure(after_length, monkeypatch, reason):
    session = after_length
    rt = session.main
    assert not rt.compactor.compaction_due()
    rt.messages[-1].usage.input = 61_440
    assert rt.compactor.compaction_due()
    install_responses(monkeypatch, AssistantMessage([TextContent("complete checkpoint")]))
    assert rt.compact(reason=reason)["summary"] == "complete checkpoint"
    # Growth in this same run must allow ordinary compaction again, below the safety reserve.
    rt.messages.append(AssistantMessage(usage=Usage(input=57_196)))
    assert rt.compactor.compaction_due()
    install_responses(monkeypatch, length_limited())
    rt.compact(reason="auto")
    assert [c["max_tokens"] for c in summary_calls(session)][-1] == 5120


@pytest.mark.parametrize("reset", ["set_model", "window_only", "prepare_run", "new_prompt"])
def test_length_failure_resets_at_runtime_boundaries(after_length, monkeypatch, reset):
    session = after_length
    rt = session.main
    assert not rt.compactor.compaction_due()
    if reset == "set_model":
        session.set_model("faux/other")
        session.set_model("faux/test")
    elif reset == "window_only":
        original = rt.model
        rt.model = replace(original, context_window=original.context_window - 1)
        rt.model = original
    elif reset == "prepare_run":
        rt.prepare_run()
    else:
        install_responses(monkeypatch, length_limited(), AssistantMessage([TextContent("done")]))
        session.prompt("Continue")
        assert session.main.agent.last_text == "done"
        assert [row["max_tokens"] for row in summary_calls(session)] == [5120, 5120]
        return
    assert rt.compactor.compaction_due()


def test_length_failure_resets_on_retry_fallback(make_session, monkeypatch):
    session = make_session(settings={
        "providers": OC_PROVIDERS,
        "compaction": {"triggerTokens": 1, "pruneRatio": 0.0},
        "retry": {"fallbackModel": "faux/fallback"},
    })
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)
    overloaded = AssistantMessage(stop_reason="error", error_kind="retryable", error_message="overloaded")
    seen = install_responses(monkeypatch, length_limited(), overloaded, overloaded,
                             AssistantMessage([TextContent("checkpoint")]), AssistantMessage([TextContent("done")]))
    session.prompt("Original task")
    assert session.main.agent.last_text == "done" and seen[-1][0].spec == "faux/fallback"
    assert [row["max_tokens"] for row in summary_calls(session)] == [5120, 5120]


def test_successful_length_recovery_keeps_one_reactive_overflow_guard(make_session, monkeypatch):
    session = make_session(settings={"compaction": {"triggerTokens": 1, "pruneRatio": 0.0}})
    overflow = AssistantMessage(stop_reason="error", error_kind="overflow", error_message="context is too long")
    install_responses(monkeypatch, length_limited(), overflow, AssistantMessage([TextContent("checkpoint")]), overflow)
    session.prompt("Original task")
    assert session.main.messages[-1].error_message == "context is too long"
    assert session.store.last_compaction()["reason"] == "overflow"
    ordinary, recovery = summary_calls(session)
    assert recovery["max_tokens"] < ordinary["max_tokens"]


@pytest.mark.parametrize(("input_tokens", "target"), [(1500, 375), (80_000, 8000)])
def test_summary_budget_tracks_evicted_input(make_session, monkeypatch, input_tokens, target):
    session = make_session(faux.reply("acknowledged"), settings={"compaction": {"keepRecentTokens": 0}})
    session.prompt("x" * (input_tokens * 4))
    model = replace(session.model, context_window=1_000_000, max_tokens=32_768)
    seen = install_responses(monkeypatch, AssistantMessage([TextContent("verified state")]))
    assert session.main.compact(model=model)
    [(sent_model, ctx)] = seen
    assert sent_model is model
    prompt = ctx.messages[-1].text()
    assert f"{target} visible tokens" in prompt
    assert "Omit step-by-step history and verification narration." in prompt
    assert "Do NOT restate system/developer instructions, sandbox boundaries" in prompt
    assert "only when explicitly reported by a tool output marker" in prompt


@pytest.mark.parametrize("scenario", ["sufficient", "insufficient", "prune-only"])
def test_auto_compaction_plans_prune_before_persisting(make_session, monkeypatch, scenario):
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 40}})
    runtime = session.main
    runtime.model = replace(runtime.model, context_window=64_000)
    needle = "The exact rejected receipt is PAX-38M7."
    messages = [UserMessage("Investigate the rejected delivery.")]
    for i in range(4):
        path = f"log-{i}"
        content = ("history " * 2800 + needle) if i == 0 else ("data " * 4500 if i == 1 else "ok")
        messages.extend([
            AssistantMessage([ToolCall(path, "read", {"path": path})], stop_reason="tool_use"),
            ToolResultMessage(path, "read", [TextContent(content)]),
        ])
    messages.append(AssistantMessage([TextContent("Ready for the next instruction. " * 30)]))
    for message in messages:
        runtime.messages.append(message)
        runtime.ids.append(session.store.append_message(message))
    offset = {"sufficient": -2000, "insufficient": -12_000, "prune-only": 3000}[scenario]
    session.settings.set("compaction.triggerTokens", runtime.context_tokens() + offset)
    assert runtime.compactor.compaction_due() is (scenario != "prune-only")
    seen = install_responses(monkeypatch, AssistantMessage([TextContent(needle)]))

    assert runtime.compactor.before_request() == "ok"
    reopened = SessionStore.load(session.store.path)
    prunes = [entry for entry in reopened.entries if entry.get("mode") == "prune"]
    if scenario == "insufficient":
        assert prunes == []
        _, ctx = seen[0]
        assert any(needle in message.text() for message in ctx.messages if isinstance(message, ToolResultMessage))
        assert needle in reopened.last_compaction(mode="summary")["summary"]
    else:
        assert len(prunes) == 1
        assert len(prunes[0]["pruned"]) == 2
        assert not seen
        assert reopened.last_compaction(mode="summary") is None


def test_in_context_compaction_preserves_hosted_web_tools_and_falls_back_if_invoked(make_session, monkeypatch):
    from virgo.ai.types import Model

    session = make_session(settings={"compaction": {"enabled": False}})
    codex_model = Model("openai-codex", "gpt-6.2-sol", "openai-codex", reasoning=True)
    session.main.model = session.model = codex_model
    session.small_model = Model("openai-codex", "gpt-6-luna", "openai-codex", reasoning=True)
    session.main.messages.extend([UserMessage("Inspect repo"), AssistantMessage([TextContent("Inspected")])])

    expected_web_tools = session.main.request_context(session.main.messages, codex_model).web_tools
    assert expected_web_tools == [{"type": "web_search"}]

    # 1. Normal in-context summary must keep the exact hosted web_tools prefix to stay cache-warm.
    seen = install_responses(monkeypatch, AssistantMessage([TextContent("## Goal & Constraints\nKept")]))
    res = session.compact()
    assert res is not None and res["strategy"] == "in_context"
    assert seen[0][1].web_tools == expected_web_tools

    # 2. If the model unexpectedly triggers a hosted web tool during in-context summarization, fall back.
    session.main.messages.append(UserMessage("More work"))
    session.main.messages.append(AssistantMessage([TextContent("More progress")]))
    seen_fallback = install_responses(
        monkeypatch,
        AssistantMessage([TextContent("Searched")], native_started=True),
        AssistantMessage([TextContent("## Goal & Constraints\nSerialized fallback")]),
    )
    res2 = session.compact()
    assert res2 is not None and res2["strategy"] == "serialized"
    assert "Serialized fallback" in res2["summary"]
    assert seen_fallback[0][1].web_tools == expected_web_tools
    assert seen_fallback[1][0] is session.small_model


def test_in_context_compaction_retries_retryable_errors_and_falls_back(make_session, monkeypatch):
    session = make_session(
        faux.reply("earlier work"),
        settings={"compaction": {"enabled": False}, "retry": {"maxRetries": 2}},
    )
    session.prompt("Build the parser")
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)

    # 1. Transient retryable overload recovers on retry without recording persistent diagnostic pollution.
    overload = AssistantMessage(
        stop_reason="error", error_kind="retryable",
        error_message="Our servers are currently overloaded. Please try again later.",
    )
    seen = install_responses(monkeypatch, overload, AssistantMessage([TextContent("## Goal & Constraints\nRecovered")]))
    res = session.compact()
    assert res is not None and res["strategy"] == "in_context"
    assert len(seen) == 2
    assert session.diagnostics.recent() == []

    # 2. Exhausted retryable errors fall back to serialized summary cleanly.
    session.main.messages.extend([UserMessage("Next step"), AssistantMessage([TextContent("Step done")])])
    seen2 = install_responses(
        monkeypatch,
        overload, overload, overload,
        AssistantMessage([TextContent("## Goal & Constraints\nFallback summary")]),
    )
    res2 = session.compact()
    assert res2 is not None and res2["strategy"] == "serialized"
    assert "Fallback summary" in res2["summary"]
    assert len(seen2) == 4
    assert seen2[-1][0] is session.small_model
    assert session.diagnostics.recent() == []


def test_auto_compaction_preserves_unread_tool_results_and_prevents_thrashing(make_session, project, monkeypatch):
    (project / "spec.txt").write_text("KEY=ALPHA-99\n" + ("filler line\n" * 1000), encoding="utf-8")
    session = make_session(
        persist=True,
        settings={
            "providers": {"faux": {"models": {"test": {"contextWindow": 128_000}}}},
            "compaction": {"keepRecentTokens": 250, "pruneRatio": 0.0},
        },
    )
    rt = session.main
    # Trigger right above baseline so a single large read crosses triggerTokens while staying well below window.
    session.settings.set("compaction.triggerTokens", rt.context_tokens() + 500)
    seen = install_responses(
        monkeypatch,
        AssistantMessage([ToolCall("r1", "read", {"path": "spec.txt"})], stop_reason="tool_use"),
        AssistantMessage([ToolCall("r2", "read", {"path": "spec.txt"})], stop_reason="tool_use"),
        AssistantMessage([TextContent("## Goal & Constraints\nEarlier read summarized")]),
        AssistantMessage([TextContent("Saw KEY=ALPHA-99")]),
    )
    session.prompt("Read spec.txt and report KEY.")

    # Call 0: first turn invokes read(spec.txt).
    # Before Call 1: only the unread read batch exists -> auto-compaction defers so Call 1 is a main
    # turn that sees KEY=ALPHA-99 directly!
    assert not _is_summary_request(seen[1][1])
    assert any(
        isinstance(m, ToolResultMessage) and "KEY=ALPHA-99" in m.text() for m in seen[1][1].messages
    )
    # Call 1 invokes a second read -> now [r1, ToolRes1] is already read and gets summarized (Call 2),
    # while the unread [r2, ToolRes2] batch is retained intact for Call 3!
    assert _is_summary_request(seen[2][1])
    assert any(
        isinstance(m, ToolResultMessage) and m.tool_call_id == "r2" and "KEY=ALPHA-99" in m.text()
        for m in seen[3][1].messages
    )
    entry = session.store.last_compaction(mode="summary")
    assert entry is not None and len(entry["kept_ids"]) >= 2
    assert rt.agent.last_text == "Saw KEY=ALPHA-99"
