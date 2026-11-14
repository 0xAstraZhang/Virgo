"""Harness: persistence, append-only requests, commands, hooks, extensions, notifications."""

from __future__ import annotations

import json
import shutil
import textwrap
import threading

from virgo.agent import AgentEvent, AgentTool, ToolResult
from virgo.ai.providers import faux
from virgo.ai.stream import StreamEvent
from virgo.ai.types import (
    AssistantMessage,
    ImageContent,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
)
from virgo.cli import main as cli
from virgo.harness.session import AgentSession, Runtime
from virgo.harness.settings import Paths
from virgo.harness.store import SessionStore

from tests.support import REPO_ROOT
from tests.support.harness import RecordingUI, calls
from tests.support.shell import selected


def test_session_persists_and_resumes(make_session, project):
    s = make_session(
        faux.reply(tools=[("read", {"path": "nope.txt"})]), faux.reply("first answer"), persist=True
    )
    s.prompt("hello")
    path = s.store.path
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines[0]["type"] == "session" and all(
        e["parent_id"] == lines[i]["id"] for i, e in enumerate(lines[2:], 1)
    )
    faux.script("test", faux.reply("second answer"))
    s2 = AgentSession(project, ui=RecordingUI(), model="faux/test", session=path, permission_mode="accept-all")
    assert [type(m).__name__ for m in s2.main.messages] == [type(m).__name__ for m in s.main.messages]
    s2.prompt("again")
    assert s2.main.agent.last_text == "second answer"
    assert SessionStore.list(path.parent)[0]["title"] == "hello"
    s2.shutdown()


def test_tool_completion_order_survives_reload_without_changing_model_history(make_session):
    release_slow = threading.Event()

    def slow(args, ctx):
        assert release_slow.wait(2), "fast completion must reach the transcript before the slow one"
        return ToolResult([TextContent("slow result"), ImageContent("aGVsbG8=", "image/png")])

    session = make_session(
        faux.reply(tools=[("slow", {}, "slow-id"), ("fast", {}, "fast-id")]),
        faux.reply("done"),
        persist=True,
    )
    runtime = Runtime(
        session, "main", "main", store=session.store,
        tools=[
            AgentTool("slow", "slow", {"type": "object"}, slow),
            AgentTool("fast", "fast", {"type": "object"}, lambda args, ctx: "fast result"),
        ],
    )

    def release_on_fast_result(event):
        if event.type == "tool_execution_end" and event.tool_call_id == "fast-id":
            release_slow.set()

    runtime.agent.subscribe(release_on_fast_result)
    runtime.agent.prompt("go")
    restored = SessionStore.load(session.store.path)
    records = restored.custom("transcript_tool_result")
    assert [record["tool_call_id"] for record in records] == ["fast-id", "slow-id"]
    assert all(record["agent"] == "main" and record["args"] == {} for record in records)
    result = message_from_dict(records[1]["message"])
    assert result.tool_name == "slow" and result.text() == "slow result"
    assert isinstance(result.content[1], ImageContent)
    assert result.details["duration"] >= 0 and not result.is_error
    context, ids = restored.build_context()
    assert context == runtime.messages
    assert ids == runtime.ids
    assert [message.tool_call_id for message in context if isinstance(message, ToolResultMessage)] == [
        "slow-id", "fast-id",
    ]


def test_run_boundaries_preserve_notification_runs_without_entering_context(make_session):
    session = make_session(faux.reply("First answer"), faux.reply("Notification answer"), persist=True)
    session.prompt("First request")
    session.notify("Background task finished", source="jobs")
    assert session.run_pending()
    restored = SessionStore.load(session.store.path)
    assert [entry.get("kind", entry["type"]) for entry in restored.branch()] == [
        "transcript_run_start", "message", "transcript_stream", "message", "transcript_run_end",
        "transcript_run_start", "message", "transcript_stream", "message", "transcript_run_end",
    ]
    assert restored.custom("transcript_run_start") == [{"agent": "main"}, {"agent": "main"}]
    assert restored.custom("transcript_run_end") == [{"agent": "main"}, {"agent": "main"}]
    messages, ids = restored.build_context()
    assert messages == session.main.messages
    assert ids == session.main.ids
    assert len(messages) == 4 and messages[2].kind == "notification"


def test_stream_history_records_block_arrival_and_native_completion_without_token_snapshots(make_session):
    session = make_session(persist=True)
    message = AssistantMessage()

    def emit(kind, index):
        session.main._on_event(AgentEvent(
            "message_update", stream_event=StreamEvent(kind, message, index),
        ))

    message.content.append(ThinkingContent("Plan"))
    emit("thinking_start", 0)
    emit("thinking_delta", 0)
    message.content.append(TextContent("Checking"))
    emit("text_start", 1)
    message.content.append(ToolCall("local", "read", {"path": "file.txt"}))
    emit("toolcall_start", 2)
    native = RawContent("openai-responses", {
        "type": "web_search_call", "id": "search", "status": "in_progress", "action": {"query": "news"},
    })
    message.content.append(native)
    emit("native_tool_start", 3)
    message.content.append(TextContent("While the search runs"))
    emit("text_start", 4)
    native.data["status"] = "searching"
    emit("native_tool_delta", 3)
    native.data["status"] = "completed"
    emit("native_tool_end", 3)
    for kind, index in (("text_delta", 4), ("text_end", 4), ("toolcall_delta", 2), ("thinking_end", 0)):
        emit(kind, index)
    emit("retry", -1)
    session.main._on_event(AgentEvent("message_end", message=message))

    restored = SessionStore.load(session.store.path)
    records = restored.custom("transcript_stream")
    assert [(record["stream_type"], record["index"]) for record in records] == [
        ("thinking_start", 0), ("text_start", 1), ("toolcall_start", 2),
        ("native_tool_start", 3), ("text_start", 4), ("native_tool_delta", 3),
        ("native_tool_end", 3), ("retry", -1),
    ]
    assert all(set(record) == {"agent", "stream_type", "index", "block"} for record in records)
    assert [records[index]["block"]["data"]["status"] for index in (3, 5, 6)] == [
        "in_progress", "searching", "completed",
    ]
    assert records[-1]["block"] is None
    assert restored.build_context()[0] == [message]


def test_child_stream_events_do_not_enter_main_transcript_history(make_session, project):
    session = make_session()
    child_store = SessionStore.new(None, project)
    child = Runtime(session, "worker", "subagent", store=child_store, tools=[])
    child._on_event(AgentEvent("message_update", agent="worker", stream_event=StreamEvent(
        "text_start", AssistantMessage([TextContent("Child reply")]), 0,
    )))
    assert not child_store.custom("transcript_stream")
    assert not session.store.custom("transcript_stream")


def test_session_numbers_outside_the_list_are_rejected(make_session, project, monkeypatch, capsys):
    s = make_session(faux.reply("one"), persist=True)
    s.prompt("first conversation")
    saved = s.store.path
    s.new_session()
    for choice in ("0", "-1", "2", "x"):  # 0 and -1 used to open the last listed session
        s.prompt(f"/sessions {choice}")
        assert s.store.path != saved
    assert s.ui.text.count("Invalid session number.") == 4
    s.prompt("/sessions 1")
    assert s.store.path == saved

    for choice, expected in (("0", None), ("-1", None), ("2", None), ("1", saved)):
        monkeypatch.setattr("builtins.input", lambda _prompt, choice=choice: choice)
        assert cli._pick_session(saved.parent) == expected
    capsys.readouterr()


def test_moved_version_one_session_resumes(make_session, project, tmp_path):
    previous = tmp_path / "previous-project" / "saved.jsonl"
    previous.parent.mkdir()
    records = [
        {"type": "session", "version": 1, "id": "previous", "cwd": str(previous.parent)},
        {
            "type": "message",
            "id": "u1",
            "parent_id": None,
            "message": {"role": "user", "content": "remember the earlier task"},
        },
    ]
    original = "".join(json.dumps(row) + "\n" for row in records)
    previous.write_text(original)
    sessions = Paths(project).sessions
    sessions.mkdir(parents=True)
    migrated = sessions / previous.name
    shutil.copy2(previous, migrated)
    session = make_session(faux.reply("resumed"), session="continue")
    assert session.store.path == migrated and session.store.id == "previous"
    assert session.main.messages[0].text() == "remember the earlier task"
    session.prompt("continue")
    assert session.main.agent.last_text == "resumed"
    assert migrated.read_text().startswith(original)
    assert previous.read_text() == original


def test_requests_are_append_only(make_session):
    s = make_session(
        faux.reply(tools=[("bash", {"command": "echo 1"})]),
        faux.reply("done"),
        faux.reply(tools=[("glob", {"pattern": "*"})]),
        faux.reply("done again"),
    )
    s.prompt("one")
    s.notify("job finished", source="jobs")
    s.run_pending()
    reqs = calls()
    assert len(reqs) == 4
    for a, b in zip(reqs, reqs[1:], strict=False):
        assert a["system"] == b["system"] and a["tools"] == b["tools"]
        assert b["messages"][: len(a["messages"])] == a["messages"]


def test_plan_mode_exposes_read_only_tools_and_changes_back_on_switch(make_session):
    # PowerShell is intentionally unavailable in plan mode (covered by shell command tests).
    s = make_session(faux.reply("The plan."), faux.reply("Implementation."), mode="plan",
                     settings={"shell": {"executable": "bash"}})
    s.prompt("Plan the change")
    planned = calls()[0]
    assert "Plan mode is active" in planned["system"]
    assert {"read", "grep", "glob", "bash"}.issubset(planned["tools"])
    assert not {"write", "edit", "workflow", "schedule", "team_review"}.intersection(planned["tools"])
    s.permission_mode = "accept-all"
    s.prompt("Implement it")
    implementation = calls()[1]
    assert "Plan mode is active" not in implementation["system"]
    assert {"write", "edit"}.issubset(implementation["tools"])
    assert implementation["messages"][: len(planned["messages"])] == planned["messages"]


def test_plan_switch_blocks_writes_that_already_passed_batch_preflight(make_session, project):
    s = make_session(
        faux.reply(tools=[
            ("write", {"path": "first.txt", "content": "one"}),
            ("write", {"path": "second.txt", "content": "two"}),
        ]),
        faux.reply("Plan only."),
        mode="accept-all",
    )

    def switch_after_preflight(event, session):
        if event.args.get("path") == "second.txt":
            session.permission_mode = "plan"

    s.bus.on("tool_call", switch_after_preflight)
    s.prompt("write two files")
    assert not (project / "first.txt").exists()
    assert not (project / "second.txt").exists()
    results = [message for message in s.main.messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 2
    assert all(result.is_error and "Plan mode" in result.text() for result in results)


def test_commands_and_templates(make_session, project):
    (project / ".virgo" / "prompts").mkdir(parents=True)
    (project / ".virgo" / "prompts" / "fix.md").write_text(
        "---\ndescription: fix an issue\n---\nFix issue $1 in ${2:-main}. $@"
    )
    ui = RecordingUI()
    s = make_session(faux.reply("ok"), ui=ui)
    s.prompt("/help")
    assert "/fix" in ui.text and "/compact" in ui.text
    s.prompt("/fix 42")
    assert calls()[0]["messages"][0]["content"] == "Fix issue 42 in main. 42"
    s.prompt("/nope")
    assert "Unknown command" in ui.text


def test_command_hooks(make_session, python_command):
    hook = python_command(
        textwrap.dedent("""
        import json, sys
        data = json.load(sys.stdin)
        if "forbidden" in data["tool_input"].get("command", ""):
            print("no forbidden commands", file=sys.stderr)
            sys.exit(2)
    """)
    )
    settings = {"hooks": {"PreToolUse": [{"matcher": "bash|powershell", "command": hook}]}}
    s = make_session(settings=settings)
    tool_name = selected(s).tool_name
    faux.push(
        "test",
        faux.reply(tools=[(tool_name, {"command": "echo forbidden"}), (tool_name, {"command": "echo fine"})]),
        faux.reply("ok"),
    )
    s.prompt("go")
    r = [m for m in s.main.messages if isinstance(m, ToolResultMessage)]
    assert r[0].is_error and "no forbidden commands" in r[0].text()
    assert r[1].text() == "fine"


def test_user_extension_registers_tool_and_hook(make_session, project):
    ext = project / ".virgo" / "extensions"
    ext.mkdir(parents=True)
    (ext / "hello.py").write_text(
        textwrap.dedent("""
        from virgo.agent import AgentTool, ToolResult
        def setup(api):
            api.register_tool(AgentTool("hello", "say hi", {"type": "object"}, lambda a, c: "hi from ext", read_only=True))
            api.on("input", lambda text, s: text.replace("SECRET", "[redacted]"))
    """)
    )
    s = make_session(faux.reply(tools=[("hello", {})]), faux.reply("ok"))
    s.prompt("my SECRET plan")
    assert calls()[0]["messages"][0]["content"] == "my [redacted] plan"
    assert "hello" in calls()[0]["tools"]
    assert any(isinstance(m, ToolResultMessage) and m.text() == "hi from ext" for m in s.main.messages)


def test_notify_wakes_idle_session(make_session):
    ui = RecordingUI()
    s = make_session(faux.reply("handled notification"), ui=ui)
    s.notify("background thing done", source="jobs")
    assert ui.woken.is_set() and len(s.wakeups) == 1
    assert s.run_pending()
    first = calls()[0]["messages"][0]
    assert '<notification source="jobs">' in first["content"][0]["text"]


def test_examples_directory_loads(make_session):
    examples = REPO_ROOT / "examples" / "project"
    ui = RecordingUI()
    s = make_session(faux.reply("ok"), cwd=examples, ui=ui)
    assert "stats" in s.commands.names() and "review" in s.templates
    assert "code-review" in s.services["skills"] and "reviewer" in s.services["subagents"].types
    s.prompt("hi")
    system = calls()[0]["system"]
    assert "review-changes" in system and "code-review" in system
    assert not [line for line in ui.lines if "failed" in line]


def test_loop_notices_are_not_recorded_as_model_calls(make_session):
    session = make_session(faux.reply(tools=[("glob", {"pattern": "*"})]))
    child = Runtime(session, "worker", "subagent", max_turns=1)
    child.agent.prompt([UserMessage("go")])
    assert "turn limit" in child.messages[-1].text()
    records = session.store.custom("model_call")
    assert len(records) == 1 and records[0]["agent"] == "worker"


def test_post_tool_hook_feedback_keeps_image_results(make_session, project, python_command):
    (project / "pixel.png").write_bytes(b"\x89PNG fake")
    command = python_command("import sys; print('checked', file=sys.stderr); sys.exit(2)")
    hook = {"matcher": "read", "command": command}
    session = make_session(
        faux.reply(tools=[("read", {"path": "pixel.png"})]), faux.reply("seen"),
        settings={"hooks": {"PostToolUse": [hook]}},
    )
    session.prompt("look at the image")
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert any(isinstance(block, ImageContent) for block in result.content)
    assert result.text().endswith("\n\n[hook feedback] checked")


def test_post_tool_hook_feedback_keeps_terminate(make_session, python_command):
    from virgo.agent.types import AgentTool
    from virgo.ai.providers.faux import CALLS

    command = python_command("import sys; print('noted', file=sys.stderr); sys.exit(2)")
    hook = {"matcher": "finish", "command": command}
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
