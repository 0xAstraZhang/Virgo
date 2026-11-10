"""Agent core: loop semantics independent of the harness."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from virgo import ai
from virgo.agent import Agent, AgentConfig, AgentTool, Block, Continue, ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, ToolResultMessage, UserMessage


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
            log.append(("start", name, time.time()))
        time.sleep(delay)
        if log is not None:
            log.append(("end", name, time.time()))
        return f"{name}:{args.get('x', '')}"

    return AgentTool(
        name, "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, run, sequential=sequential
    )


def test_tool_calls_and_results_are_paired_in_order():
    agent, _ = make_agent(
        [echo_tool("a"), echo_tool("b")],
        faux.reply(tools=[("a", {"x": "1"}, "c1"), ("b", {"x": "2"}, "c2")]),
        faux.reply("done"),
    )
    agent.prompt("go")
    kinds = [type(m).__name__ for m in agent.messages]
    assert kinds == [
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "ToolResultMessage",
        "AssistantMessage",
    ]
    assert [m.tool_call_id for m in agent.messages if isinstance(m, ToolResultMessage)] == ["c1", "c2"]
    assert agent.messages[2].text() == "a:1"
    assert agent.last_text == "done"


def test_parallel_by_default_and_sequential_flag():
    log = []
    agent, _ = make_agent(
        [echo_tool("a", 0.2, log=log), echo_tool("b", 0.2, log=log)],
        faux.reply(tools=[("a", {}), ("b", {})]),
        faux.reply("ok"),
    )
    t0 = time.time()
    agent.prompt("go")
    assert time.time() - t0 < 0.38  # ran concurrently
    log.clear()
    agent2, _ = make_agent(
        [echo_tool("a", 0.1, sequential=True, log=log), echo_tool("b", 0.1, log=log)],
        faux.reply(tools=[("a", {}), ("b", {})]),
        faux.reply("ok"),
    )
    agent2.prompt("go")
    starts = [e for e in log if e[0] == "start"]
    ends = [e for e in log if e[0] == "end"]
    assert starts[1][2] >= ends[0][2]  # second started after first ended


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


def test_length_stop_rejects_possibly_truncated_tool_calls():
    agent, _ = make_agent(
        [echo_tool("a")], faux.reply(tools=[("a", {})], stop_reason="length"), faux.reply("again")
    )
    agent.prompt("go")
    res = next(m for m in agent.messages if isinstance(m, ToolResultMessage))
    assert res.is_error and "truncated" in res.text()


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


def test_after_tool_call_overrides_result():
    agent, _ = make_agent(
        [echo_tool("a")],
        faux.reply(tools=[("a", {"x": "1"})]),
        faux.reply("fin"),
        after_tool_call=lambda ev, res: ToolResult.text("patched"),
    )
    agent.prompt("go")
    assert next(m for m in agent.messages if isinstance(m, ToolResultMessage)).text() == "patched"


def test_steering_is_delivered_before_next_model_call():
    tool_ran = threading.Event()

    def slow(args, ctx):
        tool_ran.set()
        time.sleep(0.2)
        return "slow done"

    tool = AgentTool("slow", "x", {"type": "object"}, slow)
    agent, _ = make_agent([tool], faux.reply(tools=[("slow", {})]), faux.reply("saw steering"))
    th = threading.Thread(target=agent.prompt, args=("go",))
    th.start()
    tool_ran.wait(2)
    agent.steer("actually, also do X")
    th.join(5)
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


def test_abort_stops_streaming():
    agent, _ = make_agent([], faux.reply("never", delay=2))
    th = threading.Thread(target=agent.prompt, args=("go",))
    th.start()
    time.sleep(0.1)
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
