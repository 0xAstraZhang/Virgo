"""Regression tests for reliability fixes found in a whole-project review."""

from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
import types

import pytest

from virgo.agent.types import ToolResult
from virgo.ai.providers import faux
from virgo.ai.types import ImageContent, TextContent, ToolResultMessage, UserMessage
from virgo.cli import repl
from virgo.features import background
from virgo.features.tasks import TaskStore
from virgo.features.workflow import WorkflowError, run_workflow
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.session import Runtime
from virgo.tools.truncate import MAX_LINES, budget

from .test_repl import ReplUI, ScriptedBroker


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def pending_work(session) -> bool:
    return any(session.bus.emit("pending_work", session))


# -- harness ------------------------------------------------------------------------


def test_subagent_system_prompt_lists_only_its_own_tools(make_session):
    seen = {}

    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            seen.update(system=ctx.system, tools=[t.name for t in ctx.tools])
            return faux.reply("sub done")
        return faux.reply("main done")

    session = make_session()
    faux.push("test", faux.reply(tools=[("agent", {"description": "look", "prompt": "find", "type": "explore"})]))
    faux.handler("test", respond)
    session.prompt("delegate")
    listed = seen["system"].split("<tools>\n", 1)[1].split("\n</tools>", 1)[0]
    names = [line[2:].split(":", 1)[0] for line in listed.splitlines()]
    assert sorted(names) == sorted(seen["tools"]) == ["bash", "glob", "grep", "read"]


def test_loop_notices_are_not_recorded_as_model_calls(make_session):
    session = make_session(faux.reply(tools=[("glob", {"pattern": "*"})]))
    child = Runtime(session, "worker", "subagent", max_turns=1)
    child.agent.prompt([UserMessage("go")])
    assert "turn limit" in child.messages[-1].text()
    records = session.store.custom("model_call")
    assert len(records) == 1 and records[0]["agent"] == "worker"


def test_post_tool_hook_feedback_keeps_image_results(make_session, project):
    (project / "pixel.png").write_bytes(b"\x89PNG fake")
    hook = {"matcher": "read", "command": "echo checked >&2; exit 2"}
    session = make_session(
        faux.reply(tools=[("read", {"path": "pixel.png"})]), faux.reply("seen"),
        settings={"hooks": {"PostToolUse": [hook]}},
    )
    session.prompt("look at the image")
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert any(isinstance(block, ImageContent) for block in result.content)
    assert result.text().endswith("\n\n[hook feedback] checked")


# -- tools ----------------------------------------------------------------------------


def test_budget_reports_truncation_only_when_text_is_cut(tmp_path):
    exact = "".join(f"{i}\n" for i in range(MAX_LINES))
    assert budget(exact, tmp_path, "t") == exact
    assert not (tmp_path / ".virgo").exists()
    longer = "".join(f"{i}\n" for i in range(MAX_LINES + 5))
    assert f"showing the first {MAX_LINES} of {MAX_LINES + 5} lines" in budget(longer, tmp_path, "t")
    assert f"showing the last {MAX_LINES} of {MAX_LINES + 5} lines" in budget(longer, tmp_path, "t", keep="tail")


# -- background jobs --------------------------------------------------------------------


def test_job_that_cannot_start_is_not_left_pending(make_session, project, monkeypatch):
    session = make_session()
    jobs = session.services["jobs"]

    def broken(*args, **kwargs):
        raise OSError("no shell")

    monkeypatch.setattr(background, "spawn", broken)
    with pytest.raises(OSError):
        jobs.start_command("echo hi", project)
    assert not jobs.jobs and not pending_work(session)
    assert not list((project / ".virgo" / "outputs").glob("*.log"))


def test_finished_job_is_pending_until_its_notification_is_queued(make_session, project, monkeypatch):
    session = make_session()
    jobs = session.services["jobs"]
    entered, release = threading.Event(), threading.Event()
    notify = session.notify

    def slow_notify(*args, **kwargs):
        entered.set()
        release.wait(5)
        notify(*args, **kwargs)

    monkeypatch.setattr(session, "notify", slow_notify)
    job = jobs.start_command("true", project)
    assert entered.wait(5)
    assert job.status == "completed" and pending_work(session)  # print mode must keep waiting
    release.set()
    assert wait_for(lambda: not pending_work(session))
    assert session.wakeups


# -- teams ------------------------------------------------------------------------------


def test_message_arriving_as_a_teammate_turn_ends_is_delivered(make_session):
    seen = []

    def respond(ctx):
        if "You are a Virgo teammate" in ctx.system:
            seen.append(str(ctx.messages[-1].content))
        return faux.reply("ok")

    session = make_session()
    faux.handler("test", respond)
    team = session.services["team"]
    sent = []

    def late_message(rt):
        # After the loop's last steering check: only the teammate loop can deliver it.
        if rt.role == "teammate" and not sent:
            sent.append(team.send("lead", "alice", "late note"))

    session.bus.on("agent_stop", late_message, priority=100)
    team.spawn("alice", "helper", "first task", False, False)
    assert wait_for(lambda: any("late note" in text for text in seen))
    team.request_stop("alice")


# -- tasks, memory, workflows -------------------------------------------------------------


def test_task_ids_cannot_name_files_outside_the_task_directory(project):
    store = TaskStore(project / ".virgo" / "tasks")
    store.create("real")
    (project / ".virgo" / "secret.json").write_text(json.dumps({"id": "x", "subject": "outside"}))
    assert store.get("../secret") is None
    with pytest.raises(ValueError):
        store.update("../secret", "main", status="completed")
    with pytest.raises(ValueError, match="unknown task"):
        store.create("dependent", blocked_by=["../secret"])


def test_memory_tidy_rejects_malformed_records_before_rewriting(make_session):
    session = make_session(faux.reply('[{"name": "a", "type": "user", "description": "d", "body": 3}]'))
    store = session.services["memory"]
    store.save("one", "user", "first", "alpha")
    store.save("two", "user", "second", "beta")
    session.prompt("/memory tidy")
    assert "Tidy skipped" in session.ui.text
    assert {m.name for m in store.all()} == {"one", "two"}


def test_unknown_workflow_or_escaping_run_id_leaves_no_journal(make_session, project):
    api = ExtensionAPI(make_session(), "test")
    with pytest.raises(WorkflowError, match="unknown workflow"):
        run_workflow(api, "missing", {}, None, threading.Event())
    assert not (project / ".virgo" / "runs").exists()
    with pytest.raises(WorkflowError, match="no run"):
        run_workflow(api, None, None, "../escape", threading.Event())


def test_workflow_tool_steps_do_not_register_agent_types(make_session, project):
    flows = project / ".virgo" / "workflows"
    flows.mkdir(parents=True)
    (flows / "probe.py").write_text("def run(ctx, args):\n    return ctx.agent('look around', tools=['read'])\n")
    session = make_session()
    faux.handler("test", lambda ctx: faux.reply("looked"))
    out = run_workflow(ExtensionAPI(session, "test"), "probe", {}, None, threading.Event())
    assert out["result"] == "looked"
    assert not any(name.startswith("wf:") for name in session.services["subagents"].types)


# -- MCP ----------------------------------------------------------------------------------


def test_malformed_mcp_config_does_not_stop_the_session(make_session, project):
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text("[]")
    session = make_session()
    assert session.services["mcp"] == {} and "mcp: invalid" in session.ui.text


def test_mcp_server_with_odd_output_cannot_stop_the_session(make_session, project):
    server = project / "odd_server.py"
    server.write_text(textwrap.dedent("""
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line)
            if "id" not in msg:
                continue
            print(42, flush=True)  # not a JSON-RPC object
            result = {"serverInfo": {}} if msg["method"] == "initialize" else {
                "tools": [{"name": "echo"}, {"description": "no name"}, "junk"]}
            if sys.argv[1] == "bad":
                result = "not an object"
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """))
    servers = {
        name: {"command": sys.executable, "args": [str(server), name], "timeout": 10} for name in ("odd", "bad")
    }
    session = make_session(settings={"mcpServers": servers})
    clients = session.services["mcp"]
    assert clients["odd"].error is None and [t["name"] for t in clients["odd"].tools] == ["echo"]
    assert session.tool("mcp__odd__echo") is not None
    assert "invalid result" in clients["bad"].error


# -- CLI ----------------------------------------------------------------------------------


def test_failing_inline_command_does_not_end_the_repl(make_session, monkeypatch):
    monkeypatch.setattr(repl, "_setup_readline", lambda *args: None)
    ui, broker = ReplUI(), ScriptedBroker()
    session = make_session(ui=ui)

    def broken(s, args):
        raise RuntimeError("status unavailable")

    session.commands.register("status", broken, concurrent=True)
    runner = threading.Thread(target=repl.run_repl, args=(session, ui, broker), daemon=True)
    runner.start()
    broker.push("line", "/status")
    assert wait_for(lambda: "status unavailable" in ui.text)
    assert runner.is_alive()
    broker.push("line", "/exit")
    runner.join(5)
    assert not runner.is_alive()


def test_print_mode_json_lines_stay_whole_across_threads(monkeypatch):
    from virgo.agent.types import AgentEvent
    from virgo.cli.console import PrintUI

    class SlowStdout:
        """Writes each record in two halves and, between them, waits for a second writer to arrive.

        Without serialisation the second thread's half lands inside the first record every time;
        with it the second writer cannot enter, so the wait times out and the record stays whole.
        """

        def __init__(self):
            self.parts: list[str] = []
            self.arrived = threading.Semaphore(0)

        def write(self, text: str) -> int:
            half = len(text) // 2
            self.parts.append(text[:half])
            self.arrived.release()  # signal only once our first half is down
            if self.arrived.acquire(timeout=0.3):  # our own release, consumed once
                self.arrived.acquire(timeout=0.3)  # the other writer's arrival, if it can get in
            self.parts.append(text[half:])
            return len(text)

        def flush(self) -> None:
            pass

    out = SlowStdout()
    monkeypatch.setattr(sys, "stdout", out)
    ui = PrintUI(json_mode=True)

    def emit(tag: str):
        result = ToolResult([TextContent(tag * 4000)])
        ui.on_event(AgentEvent("tool_execution_end", "worker", tool_call_id="c", tool_name="t", result=result))

    threads = [threading.Thread(target=emit, args=(tag,)) for tag in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = "".join(out.parts).splitlines()
    assert sorted(json.loads(line)["result"] for line in lines) == ["a" * 4000, "b" * 4000]


# -- follow-up regression tests (issues #3, #4, #5) ------------------------------------------


def test_failed_job_notification_is_reported_and_does_not_hold_print_mode(make_session, project, monkeypatch):
    session = make_session()
    jobs = session.services["jobs"]

    def broken_notify(*args, **kwargs):
        raise RuntimeError("queue gone")

    monkeypatch.setattr(session, "notify", broken_notify)
    job = jobs.start_command("true", project)
    assert wait_for(lambda: job.reported)
    assert not pending_work(session)
    assert f"could not deliver the notification for {job.id}" in session.ui.text
    assert "queue gone" in session.ui.text


@pytest.mark.parametrize("bad", [["x"], "abc", 5])
def test_malformed_mcp_servers_setting_does_not_stop_the_session(make_session, bad):
    session = make_session(settings={"mcpServers": bad})
    assert session.services["mcp"] == {}
    assert "mcp: invalid settings: mcpServers must be an object" in session.ui.text


def test_teammate_whose_runtime_cannot_start_is_stopped_and_leaves_plan_mode(make_session, monkeypatch):
    from virgo.harness import session as session_module

    session = make_session()
    team, perms = session.services["team"], session.services["permissions"]

    def broken_runtime(*args, **kwargs):
        raise RuntimeError("no runtime")

    monkeypatch.setattr(session_module, "Runtime", broken_runtime)
    with pytest.raises(RuntimeError, match="no runtime"):
        team.spawn("alice", "tester", "go", plan_first=True, worktree=False)
    assert team.members["alice"].state == "stopped"
    assert "alice" not in perms.agent_modes
    assert "alice" not in team.active_names()


def test_memory_update_with_unknown_type_on_disk_returns_a_clear_error(make_session):
    session = make_session(
        faux.reply(tools=[("memory", {"action": "update", "name": "odd", "description": "changed"})]),
        faux.reply("done"),
    )
    store = session.services["memory"]
    folder = store.dirs["project"]
    folder.mkdir(parents=True, exist_ok=True)
    original = "---\nname: odd\ndescription: old\ntype: weird\n---\n\nbody\n"
    (folder / "odd.md").write_text(original, encoding="utf-8")
    session.prompt("update the memory")
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert result.is_error
    assert "unknown type 'weird'" in result.text()
    assert (folder / "odd.md").read_text(encoding="utf-8") == original


def test_post_tool_hook_feedback_keeps_terminate(make_session):
    from virgo.agent.types import AgentTool
    from virgo.ai.providers.faux import CALLS

    hook = {"matcher": "finish", "command": "echo noted >&2; exit 2"}
    session = make_session(faux.reply(tools=[("finish", {})]), settings={"hooks": {"PostToolUse": [hook]}})
    session.register_tool(
        AgentTool(
            "finish",
            "End the turn",
            {"type": "object", "properties": {}},
            lambda args, ctx: ToolResult.text("finished", terminate=True),
        )
    )
    session.main.tools[:] = session.tools_for("main")
    session.prompt("wrap up")
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert result.text().endswith("\n\n[hook feedback] noted")
    assert len(CALLS["test"]) == 1  # terminate survived the hook: no follow-up model call


def test_openai_chat_stream_closes_thinking_block(monkeypatch):
    from virgo import ai
    from virgo.ai.providers import openai as oai
    from virgo.ai.types import Context, StreamOptions

    def chunk(**delta):
        return types.SimpleNamespace(
            usage=None,
            choices=[types.SimpleNamespace(delta=types.SimpleNamespace(tool_calls=None, **delta), finish_reason=None)],
        )

    pieces = [
        chunk(reasoning_content="think", content=None),
        chunk(reasoning_content=None, content="answer"),
    ]
    fake = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: iter(pieces)))
    )
    monkeypatch.setattr(oai, "_client", lambda model: fake)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = ai.resolve_model("openai/gpt-5")
    kinds = [ev.type for ev in oai.stream(model, Context("SYS", [UserMessage("hi")]), StreamOptions())]
    assert kinds.index("thinking_start") < kinds.index("thinking_end") < kinds.index("done")
    assert kinds.count("thinking_end") == 1
