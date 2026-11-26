"""Agent core: loop semantics independent of the harness."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from virgo import ai
from virgo.agent import Agent, AgentConfig, AgentTool, Block, Continue, ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, ToolResultMessage, UserMessage
from virgo.ai.validate import INVALID_ARGUMENTS


def make_agent(tools, *responses, **cfg) -> tuple[Agent, list]:
    faux.script("loop", *responses)
    model = ai.resolve_model("faux/loop")
    events = []

    def call(msgs, sig):
        return ai.stream(
            model, ai.Context("sys", msgs, [t.spec() for t in tools]), ai.StreamOptions(signal=sig)
        )

    agent = Agent(AgentConfig(tools=tools, call_model=call, cwd=Path.cwd(), **cfg))
    agent.subscribe(events.append)
    return agent, events


def echo_tool(name="echo", delay=0.0, sequential=False, log=None):
    def run(args, ctx):
        if log is not None:
            log.append(("start", name))
        time.sleep(delay)
        if log is not None:
            log.append(("end", name))
        return f"{name}:{args.get('x', '')}"

    return AgentTool(
        name, "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, run, sequential=sequential
    )


def test_parallel_by_default_and_sequential_flag():
    # Each tool waits for the other to arrive: run one at a time, the barrier breaks instead.
    barrier = threading.Barrier(2, timeout=2)

    def meet(args, ctx):
        barrier.wait()
        return "met"

    agent, _ = make_agent(
        [AgentTool("a", "a", {"type": "object"}, meet), AgentTool("b", "b", {"type": "object"}, meet)],
        faux.reply(tools=[("a", {}), ("b", {})]),
        faux.reply("ok"),
    )
    agent.prompt("go")
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert [(r.text(), r.is_error) for r in results] == [("met", False), ("met", False)]

    log = []
    agent2, _ = make_agent(
        [echo_tool("a", 0.05, sequential=True, log=log), echo_tool("b", 0.05, log=log)],
        faux.reply(tools=[("a", {}), ("b", {})]),
        faux.reply("ok"),
    )
    agent2.prompt("go")
    assert [kind for kind, _ in log] == ["start", "end", "start", "end"]  # never overlapped


def test_parallel_results_are_emitted_as_completed_but_history_keeps_call_order():
    release_slow = threading.Event()

    def slow(args, ctx):
        assert release_slow.wait(2), "fast result was held behind the slow tool"
        return "slow result"

    agent, events = make_agent(
        [AgentTool("slow", "slow", {"type": "object"}, slow), echo_tool("fast")],
        faux.reply(tools=[("slow", {}, "slow-id"), ("fast", {}, "fast-id")]),
        faux.reply("done"),
    )

    def release_on_fast_result(event):
        if event.type == "tool_execution_end" and event.tool_call_id == "fast-id":
            release_slow.set()

    agent.subscribe(release_on_fast_result)
    agent.prompt("go")
    assert [event.tool_call_id for event in events if event.type == "tool_execution_end"] == [
        "fast-id", "slow-id",
    ]
    results = [message for message in agent.messages if isinstance(message, ToolResultMessage)]
    assert [message.tool_call_id for message in results] == ["slow-id", "fast-id"]
    assert results[0].text() == "slow result" and not results[0].is_error


def test_parallel_interrupt_signals_workers_cancels_queue_and_joins(monkeypatch):
    ready = threading.Barrier(3, timeout=2)
    queue_cancelled = threading.Event()
    workers = {}
    saw_signal = {}
    timeline = []

    def execute(args, ctx):
        workers[ctx.call_id] = threading.current_thread()
        if ctx.call_id != "queued":
            ready.wait()
            saw_signal[ctx.call_id] = ctx.signal.wait(2)
            if saw_signal[ctx.call_id]:
                # Keep both slots occupied until the queued future is cancelled.
                queue_cancelled.wait(2)
        return "finished"

    def interrupt(futures):
        ready.wait()
        (pending,) = (future for future in futures if not future.running())
        pending.add_done_callback(lambda f: f.cancelled() and queue_cancelled.set())
        raise KeyboardInterrupt

    monkeypatch.setattr("virgo.agent.loop.as_completed", interrupt)
    agent, _ = make_agent(
        [AgentTool("wait", "wait", {"type": "object"}, execute)],
        faux.reply(tools=[("wait", {}, name) for name in ("one", "two", "queued")]),
        faux.reply("restarted"),
        max_parallel=2,
        after_tool_call=lambda ev, res: timeline.append(("after_tool_call", ev.call_id)),
    )
    agent.subscribe(lambda ev: timeline.append((ev.type, ev.tool_call_id)))
    with pytest.raises(KeyboardInterrupt):
        agent.prompt("go")

    assert saw_signal == {"one": True, "two": True}
    assert queue_cancelled.is_set()
    assert set(workers) == {"one", "two"}
    assert all(not thread.is_alive() for thread in workers.values())
    assert {call_id for kind, call_id in timeline if kind == "after_tool_call"} == {"one", "two"}
    assert timeline[-1][0] == "agent_end"
    agent.prompt("new prompt")
    assert agent.last_text == "restarted"


def test_external_abort_completes_both_active_parallel_calls():
    ready = threading.Barrier(3, timeout=2)

    def execute(args, ctx):
        ready.wait()
        if ctx.signal.wait(2):
            return ToolResult.error("aborted", details={"cancelled": True})
        return "timed out"

    agent, events = make_agent(
        [AgentTool("wait", "wait", {"type": "object"}, execute)],
        faux.reply(tools=[("wait", {}, "one"), ("wait", {}, "two")]),
    )
    prompt = threading.Thread(target=agent.prompt, args=("go",))
    prompt.start()
    try:
        ready.wait()
    finally:
        agent.abort()
        prompt.join(5)

    assert not prompt.is_alive()
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert [m.tool_call_id for m in results if m.is_error and m.details.get("cancelled")] == ["one", "two"]
    assert {ev.tool_call_id for ev in events if ev.type == "tool_execution_end"} == {"one", "two"}
    assert events[-1].type == "agent_end"
    assert len(faux.CALLS["loop"]) == 1


def test_preflight_failures_are_emitted_before_preparing_the_next_call():
    agent, events = make_agent(
        [echo_tool("valid")],
        faux.reply(tools=[("missing", {}, "bad-id"), ("valid", {}, "valid-id")]),
        faux.reply("done"),
    )
    agent.prompt("go")
    assert [(event.type, event.tool_call_id) for event in events if event.type.startswith("tool_execution_")] == [
        ("tool_execution_start", "bad-id"),
        ("tool_execution_end", "bad-id"),
        ("tool_execution_start", "valid-id"),
        ("tool_execution_end", "valid-id"),
    ]
    results = [message for message in agent.messages if isinstance(message, ToolResultMessage)]
    assert [message.tool_call_id for message in results] == ["bad-id", "valid-id"]
    assert results[0].is_error and not results[1].is_error


def test_errors_become_tool_results():
    agent, _ = make_agent(
        [echo_tool("a"), AgentTool("boom", "x", {"type": "object"}, lambda a, c: 1 / 0)],
        faux.reply(tools=[("nope", {}), ("a", {"x": 5}), ("boom", {}), ("a", {"x": {"bad": 1}})]),
        faux.reply("recovered"),
    )
    agent.prompt("go")
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert results[0].is_error and "Unknown tool" in results[0].text()
    assert not results[1].is_error and results[1].text() == "a:5"  # number coerced to string
    assert results[2].is_error and "ZeroDivisionError" in results[2].text()
    assert results[3].is_error and "Invalid arguments" in results[3].text()
    assert agent.last_text == "recovered"


def test_non_object_tool_arguments_become_an_error_result():
    # A provider sent `null` as the arguments: the model is told, the run goes on.
    agent, _ = make_agent(
        [echo_tool("a")], faux.reply(tools=[("a", {INVALID_ARGUMENTS: "null"})]), faux.reply("recovered")
    )
    agent.prompt("go")
    res = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert res.is_error and "JSON object" in res.text()
    assert agent.last_text == "recovered"


def test_length_stop_rejects_possibly_truncated_tool_calls():
    agent, _ = make_agent(
        [echo_tool("a")], faux.reply(tools=[("a", {})], stop_reason="length"), faux.reply("again")
    )
    agent.prompt("go")
    res = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert res.is_error and "truncated" in res.text()


@pytest.mark.parametrize(("text", "key"), [
    ("", "core.agent.length_continue_empty"),
    (" \n", "core.agent.length_continue_empty"),
    ("First part.", "core.agent.length_continue"),
])
def test_length_stop_continues_with_a_prompt_for_the_visible_output(text, key):
    agent, _ = make_agent([], faux.reply(text, thinking="reasoning", stop_reason="length"), faux.reply("done"))
    agent.prompt("go")
    assert agent.messages[-1].stop_reason == "stop"
    assert agent.last_text == text + "done"
    second = faux.CALLS["loop"][1]["messages"]
    assert second[-2]["stop_reason"] == "length"
    assert second[-1]["kind"] == "continue" and second[-1]["content"] == agent.cfg.t(key)


def compact_before_continuations(agent, style):
    call_model = agent.cfg.call_model

    def call(messages, signal):
        if isinstance(messages[-1], UserMessage) and messages[-1].kind == "continue":
            if style == "summary":
                messages[:] = [UserMessage("Earlier progress", kind="summary"), messages[-1]]
            else:
                messages[:] = [AssistantMessage(stop_reason="compaction")]
        return call_model(messages, signal)

    agent.cfg.call_model = call


@pytest.mark.parametrize("style", ["summary", "native"])
@pytest.mark.parametrize("continuations", [1, 2])
def test_length_continuation_survives_compaction(style, continuations):
    parts = ["first ", "second "][:continuations]
    agent, _ = make_agent([], *(faux.reply(part, stop_reason="length") for part in parts), faux.reply("end"))
    compact_before_continuations(agent, style)
    answers = []

    def record_answer(event):
        if event.type == "message_end" and isinstance(event.message, AssistantMessage):
            answers.append(agent.last_text)

    agent.subscribe(record_answer)
    agent.prompt("go")
    assert agent.last_text == "".join(parts) + "end"
    assert answers == ["".join(parts[:i + 1]) for i in range(continuations)] + [agent.last_text]


def test_last_text_uses_current_history_after_post_run_compaction():
    agent, _ = make_agent([], faux.reply("first ", stop_reason="length"), faux.reply("last"))
    agent.prompt("go")
    assert Agent(agent.cfg, messages=list(agent.messages)).last_text == "first last"

    summary = UserMessage("Earlier progress", kind="summary")
    agent.messages[:] = [summary, replace(agent.messages[-1])]
    assert agent.last_text == "last"
    agent.messages[:] = [summary]
    assert agent.last_text == ""


@pytest.mark.parametrize("reason", ["tool_use", "error", "aborted"])
def test_compacted_length_continuation_discards_fragments_on_interruption(reason):
    if reason == "tool_use":
        responses = [faux.reply("checking", tools=[("echo", {})]), faux.reply("terminal")]
    else:
        responses = [faux.reply("terminal", stop_reason=reason)]
    agent, _ = make_agent([echo_tool()], faux.reply("old answer", stop_reason="length"), *responses)
    compact_before_continuations(agent, "native")
    agent.prompt("go")
    if reason == "tool_use":
        assert isinstance(agent.messages[-2], ToolResultMessage) and not agent.messages[-2].is_error
    assert agent.last_text == "terminal"


def test_length_stop_exhausts_two_continuations_without_running_stop_hooks():
    should_stop = Mock(return_value=Continue("keep going"))
    truncated = faux.reply(thinking="reasoning", stop_reason="length")
    agent, _ = make_agent([], truncated, truncated, truncated, faux.error("unexpected fourth call"),
                          should_stop=should_stop)
    agent.prompt("go")
    assert len(faux.CALLS["loop"]) == 3
    assert [m.kind for m in agent.messages if isinstance(m, UserMessage)] == [None, "continue", "continue"]
    assert agent.messages[-1].stop_reason == "length"
    should_stop.assert_not_called()


def test_length_stop_recovers_into_tool_use():
    agent, _ = make_agent(
        [echo_tool()],
        faux.reply("", thinking="reasoning", stop_reason="length"),
        faux.reply(tools=[("echo", {"x": "ok"})]),
        faux.reply("finished"),
    )
    agent.prompt("go")
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert len(results) == 1 and results[0].text() == "echo:ok"
    assert agent.messages[-1].stop_reason == "stop" and agent.last_text == "finished"


def test_length_stop_delivers_queued_follow_up_after_exhaustion():
    truncated = faux.reply("abandoned", stop_reason="length")
    agent, _ = make_agent([], truncated, truncated, truncated, faux.reply("new ", stop_reason="length"),
                          faux.reply("answer"))
    agent.follow_up("new question")
    agent.prompt("go")
    requests = faux.CALLS["loop"]
    assert [r["messages"][-1]["kind"] for r in requests] == [None, "continue", "continue", None, "continue"]
    assert requests[3]["messages"][-1]["content"] == "new question"
    assert agent.last_text == "new answer"


@pytest.mark.parametrize("intervening_reason", ["stop", "tool_use", "length"])
def test_length_continuations_reset_after_a_normal_turn_or_tool_call(intervening_reason):
    truncated = faux.reply(stop_reason="length")
    tools = [("echo", {})] if intervening_reason != "stop" else []
    intervening = faux.reply("intermediate", tools=tools, stop_reason=intervening_reason)
    agent, _ = make_agent([echo_tool()], truncated, truncated, intervening, truncated, faux.reply("done"))
    if intervening_reason == "stop":
        agent.follow_up("another question")
    agent.prompt("go")
    assert len(faux.CALLS["loop"]) == 5
    assert agent.messages[-2].kind == "continue" and agent.last_text == "done"


def test_length_stop_delivers_steering_instead_of_an_automatic_prompt():
    truncated = faux.reply(stop_reason="length")
    agent, _ = make_agent(
        [], faux.reply("old direction", stop_reason="length"), truncated, truncated, faux.reply("done")
    )
    agent.steer("change direction")
    agent.prompt("go")
    users = [m for m in agent.messages if isinstance(m, UserMessage)]
    assert [m.kind for m in users] == [None, None, "continue", "continue"]
    assert users[1].text() == "change direction" and agent.last_text == "done"


def test_length_stop_steering_resets_exhausted_continuation_budget():
    truncated = faux.reply(stop_reason="length")
    agent, _ = make_agent([], truncated, truncated, truncated, truncated, faux.reply("done"))
    turns_seen = 0

    def steer_on_third_truncation(event):
        nonlocal turns_seen
        if event.type == "turn_end":
            turns_seen += 1
            if turns_seen == 3:
                agent.steer("take a shorter path")

    agent.subscribe(steer_on_third_truncation)
    agent.prompt("go")
    users = [m for m in agent.messages if isinstance(m, UserMessage)]
    assert [m.kind for m in users] == [None, "continue", "continue", None, "continue"]
    assert users[3].text() == "take a shorter path" and agent.last_text == "done"


def test_length_stop_does_not_call_model_when_can_run_fails_pushing_continue():
    runnable = True
    agent, _ = make_agent(
        [],
        faux.reply(thinking="reasoning", stop_reason="length"),
        faux.error("unexpected second call"),
        can_run=lambda: runnable,
    )

    def fail_save_on_continue(event):
        nonlocal runnable
        if event.type == "message_end" and isinstance(event.message, UserMessage) and event.message.kind == "continue":
            runnable = False

    agent.subscribe(fail_save_on_continue)
    agent.prompt("go")
    assert len(faux.CALLS["loop"]) == 1


@pytest.mark.parametrize("abort_event", ["turn_end", "turn_start"])
def test_abort_at_length_continuation_boundary_leaves_no_prompt(abort_event):
    agent, _ = make_agent([], faux.reply(thinking="reasoning", stop_reason="length"))

    def abort(event):
        if event.type == abort_event and agent.messages and agent.messages[-1].role == "assistant":
            agent.abort()

    agent.subscribe(abort)
    agent.prompt("go")
    assert agent.signal.is_set()
    assert len(faux.CALLS["loop"]) == 1
    assert agent.messages[-1].stop_reason == "length"


@pytest.mark.parametrize("limit", [1, 2])
def test_length_stop_respects_turn_limit_without_an_orphaned_prompt(limit):
    should_stop = Mock(return_value=Continue("keep going"))
    agent, _ = make_agent([], *[faux.reply("part", stop_reason="length")] * limit, max_turns=limit,
                          should_stop=should_stop)
    agent.prompt("go")
    assert len(faux.CALLS["loop"]) == limit
    assert [m.kind for m in agent.messages if isinstance(m, UserMessage)] == [None] + ["continue"] * (limit - 1)
    assert agent.messages[-1].stop_reason == "length"
    assert agent.last_text == "part" * limit
    should_stop.assert_not_called()


@pytest.mark.parametrize("reason", ["turn_limit", "error", "aborted", "refusal", "search_paused"])
def test_length_continuation_ending_without_an_answer_keeps_only_terminal_text(reason):
    agent, _ = make_agent(
        [], faux.reply("partial answer", stop_reason="length"), faux.reply("terminal", stop_reason=reason)
    )
    agent.prompt("go")
    assert agent.last_text == "terminal"


def test_before_tool_call_can_block_and_mutate():
    def before(ev):
        if ev.args.get("x") == "bad":
            return Block("nope")
        ev.args["x"] = ev.args.get("x", "") + "!"
        return None

    agent, _ = make_agent(
        [echo_tool("a")],
        faux.reply(tools=[("a", {"x": "bad"}), ("a", {"x": "ok"})]),
        faux.reply("fin"),
        before_tool_call=before,
    )
    agent.prompt("go")
    res = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert res[0].is_error and res[0].text() == "nope"
    assert res[1].text() == "a:ok!"


def test_steering_is_delivered_before_next_model_call():
    tool_ran, steered = threading.Event(), threading.Event()

    def slow(args, ctx):
        tool_ran.set()
        assert steered.wait(2), "the test never steered"
        return "slow done"

    tool = AgentTool("slow", "x", {"type": "object"}, slow)
    agent, _ = make_agent([tool], faux.reply(tools=[("slow", {})]), faux.reply("saw steering"))
    th = threading.Thread(target=agent.prompt, args=("go",))
    th.start()
    assert tool_ran.wait(2)
    agent.steer("actually, also do X")  # while the tool is still running
    steered.set()
    th.join(5)
    assert not th.is_alive()
    # the second model call saw tool result followed by the steering message
    second = faux.CALLS["loop"][1]["messages"]
    assert second[-2]["role"] == "toolResult"
    assert second[-1]["role"] == "user" and "also do X" in str(second[-1]["content"])


def test_follow_up_and_stop_hook_continue():
    stops = []

    def should_stop(msgs):
        stops.append(len(msgs))
        return Continue("keep going") if len(stops) == 1 else None

    agent, _ = make_agent(
        [], faux.reply("first"), faux.reply("second"), faux.reply("third"), should_stop=should_stop
    )
    agent.follow_up("queued follow-up")
    agent.prompt("go")
    texts = [m.text() for m in agent.messages if isinstance(m, AssistantMessage)]
    assert texts == ["first", "second", "third"]
    users = [m for m in agent.messages if isinstance(m, UserMessage)]
    assert users[1].text() == "queued follow-up"
    assert users[2].kind == "continue" and users[2].text() == "keep going"
    assert agent.last_text == "third"


def test_abort_stops_streaming():
    agent, _ = make_agent([], faux.reply("never", delay=2))
    streaming = threading.Event()
    # prompt() resets the signal when it starts: abort only once the reply is streaming.
    agent.subscribe(lambda ev: ev.type == "message_start" and ev.message.role == "assistant" and streaming.set())
    th = threading.Thread(target=agent.prompt, args=("go",))
    th.start()
    assert streaming.wait(2)
    agent.abort()
    th.join(3)
    assert not th.is_alive()
    assert agent.messages[-1].stop_reason == "aborted"


def test_max_turns():
    agent, _ = make_agent([echo_tool("a")], *[faux.reply(tools=[("a", {})]) for _ in range(5)], max_turns=2)
    agent.prompt("go")
    assert "turn limit" in agent.last_text


def test_provider_error_ends_run_without_crash():
    agent, events = make_agent([], faux.error("kaput"))
    agent.prompt("go")
    assert agent.messages[-1].stop_reason == "error"
    assert events[-1].type == "agent_end"
