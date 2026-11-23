"""Harness: persistence, append-only requests, commands, hooks, extensions, notifications."""

from __future__ import annotations

import builtins
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

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
    message_to_dict,
)
from virgo.cli import main as cli
from virgo.harness.session import AgentSession, Runtime
from virgo.harness.settings import Paths
from virgo.harness.store import SessionStore, _locked_file

from tests.support import REPO_ROOT
from tests.support.harness import TORN_MESSAGE, RecordingUI, calls, write_legacy_session
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
    s2 = AgentSession(project, ui=RecordingUI(), model="faux/test", session=path, approval_policy="never")
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


def test_sandbox_boundaries_are_compact_only_on_disk(project, tmp_path):
    store = SessionStore.new(tmp_path / "sessions", project)
    grants = {
        "read_paths": ["/project", "/shared/docs"], "write_paths": ["/project"],
        "network_allowed": False, "unrestricted": False, "web_allowed": True,
    }
    denied = [f"/home/user/.config/credentials/private-{i}" for i in range(13)]
    policy = {**grants, "read_deny_paths": denied, "write_deny_paths": denied}
    audit = {
        "provider": "sandbox", "backend": "seatbelt", "workspace": "/project", "revision": 3,
        "state": "established", "payload_started": True, "side_effects": "possible",
        "cleanup": "executor_completed", "native_support": "checked_at_launch", "windows_network": None,
    }
    result = ToolResult.text("ok", details={"duration": 0.5, "boundary": {
        **audit, "requested": policy, "effective": {"file_tools": policy, "native_process": {
            **policy, "runtime_read_dependencies": ["/usr/bin"], "ambient_read_access": "POSIX read-only",
        }},
        "runtime_read_dependencies": ["/usr/bin"], "unmanaged": ["broker", "hooks", "MCP services"],
    }})
    original = deepcopy(result.details)
    ids = []
    for i in range(12):
        message = ToolResultMessage(str(i), "bash", result.content, details=result.details)
        data = {"tool_call_id": str(i), "message": message_to_dict(message)}
        store.append("custom", kind="transcript_tool_result", data=data)
        ids.append(store.append_message(message))
        assert data["message"]["details"] == message.details == result.details == original

    assert all(m.details == original for m in store.build_context()[0])
    assert all(r["message"]["details"] == original for r in store.custom("transcript_tool_result"))
    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()[1:]]
    boundaries = [row.get("data", row)["message"]["details"]["boundary"] for row in rows]
    assert len(boundaries) == 24
    before = len(json.dumps(original["boundary"], ensure_ascii=False).encode()) * len(boundaries)
    after = sum(len(json.dumps(b, ensure_ascii=False).encode()) for b in boundaries)
    assert after <= 0.30 * before, f"Persisted boundary bytes: {before} -> {after}"

    restored = SessionStore.load(store.path)
    messages, message_ids = restored.build_context()
    assert (messages, message_ids) == restored.build_full_context() and message_ids == ids
    transcript = [r["message"]["details"] for r in restored.custom("transcript_tool_result")]
    assert len(messages) == len(transcript) == 12
    assert transcript == [{"duration": 0.5, "boundary": audit}] * 12
    for message in messages:
        assert message.details == {"duration": 0.5, "boundary": {**audit, "effective": {"native_process": grants}}}


def test_store_preserves_extension_result_payloads(project, tmp_path):
    store = SessionStore.new(tmp_path / "sessions", project)
    for details in (["extension", {"requested": [1]}], {"boundary": ["custom"]}, {"custom": {"effective": 3}}):
        message = ToolResultMessage("id", "extension", [], details=details)
        store.append_message(message)
        store.append("custom", kind="transcript_tool_result", data={"message": message_to_dict(message)})
    store.append("custom", kind="extension", data={"message": {"details": {"boundary": {"requested": [1]}}}})
    assert SessionStore.load(store.path).entries == store.entries


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


def test_mode_change_applies_to_next_run_not_dispatched_batch(broker_session, project):
    s = broker_session(faux.reply(tools=[
        ("write", {"path": "first.txt", "content": "one"}),
        ("write", {"path": "second.txt", "content": "two"}),
    ]), faux.reply("Done"), approval="never")
    def select_next(event, session):
        if event.args.get("path") == "second.txt":
            session.services["execution"].select_mode("read-only")
    s.bus.on("tool_call", select_next)
    s.prompt("write two files")
    assert (project / "first.txt").read_text() == "one"
    assert (project / "second.txt").read_text() == "two"
    faux.script("test", faux.reply(tools=[("write", {"path": "next.txt", "content": "blocked"})]), faux.reply("done"))
    s.prompt("new run")
    assert not (project / "next.txt").exists()
    result = [m for m in s.main.messages if isinstance(m, ToolResultMessage)][-1]
    assert result.is_error and result.details["boundary"]["dimension"] == "write_paths"


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
    assert "stats" in s.commands.names() and "inspect" in s.templates
    assert "code-review" in s.services["skills"]
    s.prompt("hi")
    system = calls()[0]["system"]
    assert "code-review" in system and "<agent_types>" not in system and "workflows" not in s.services
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


def test_reopening_legacy_session_with_search_started_succeeds(make_session, project):
    session = make_session(faux.reply("first"), faux.reply("second"), persist=True)
    session.prompt("hello")
    session.prompt("search")
    path = session.store.path
    assert path is not None
    # Simulate pre-refactor JSONL history where every assistant record contained `search_started`.
    rewritten = []
    assistant_index = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "message" and record["message"].get("role") == "assistant":
            record["message"]["search_started"] = assistant_index == 1
            if assistant_index == 1:
                record["message"].pop("native_started", None)
            assistant_index += 1
        rewritten.append(json.dumps(record))
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    restored = SessionStore.load(path)
    messages, _ = restored.build_context()
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert [m.text() for m in assistants] == ["first", "second"]
    assert [m.native_started for m in assistants] == [False, True]


@pytest.mark.parametrize("mode", ["continue", "path", "interactive"])
def test_resume_modes_recover_torn_tail(make_session, project, mode):
    directory = Paths(project).sessions
    older = directory / "older.jsonl"
    write_legacy_session(older, project, "2025-01-01", "other conversation")
    os.utime(older, (100, 100))
    store = SessionStore.new(directory, project)
    store.append_message(UserMessage("earlier task"))
    leaf = store.append_message(AssistantMessage([TextContent("earlier answer")]))
    # A late entry on an inactive branch is the last retained record, but not the leaf.
    last = store.append("custom", kind="extension", data={}, parent_id=None, active_leaf=leaf)["id"]
    path = store.path
    prefix = path.read_bytes() + b"\r\n"
    path.write_bytes(prefix + TORN_MESSAGE)
    before = set(directory.iterdir())
    if mode == "interactive":
        session = make_session(faux.reply("continued"))
        session.open_session(path)
    else:
        session = make_session(faux.reply("continued"), session="continue" if mode == "continue" else path)
    assert session.store.id == store.id and session.store.leaf == leaf
    assert path.read_bytes() == prefix
    warning, = session.ui.lines
    assert str(path) in warning or store.id in warning
    line_number = prefix.count(b"\n") + 1
    assert f"line {line_number}" in warning and "83 bytes" in warning
    assert last in warning and "unterminated" in warning.lower()
    session.prompt("next task")
    assert [msg["content"] for msg in calls()[0]["messages"] if msg["role"] == "user"] == ["earlier task", "next task"]
    assert calls()[0]["messages"][1]["content"][0]["text"] == "earlier answer"
    data = path.read_bytes()
    assert data.startswith(prefix) and set(directory.iterdir()) == before
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    assert rows[4]["parent_id"] == leaf
    clean = make_session(session=path)
    assert clean.store.id == store.id and clean.store.entries == session.store.entries and clean.ui.lines == []


@pytest.mark.parametrize(("header_only", "eol", "ending"), [
    pytest.param(False, b"\n", b"", id="entry"),
    pytest.param(True, b"\n", b"", id="header"),
    pytest.param(False, b"\n", b"  ", id="spaces"),
    pytest.param(False, b"\r\n", b"\r\n", id="crlf"),
    pytest.param(False, b"\n", b"\n\n\n", id="blanks"),
    pytest.param(False, b"\n", b"\n\t ", id="whitespace-suffix"),
])
def test_append_preserves_valid_unterminated_record(make_session, project, header_only, eol, ending):
    path = Paths(project).sessions / "saved.jsonl"
    write_legacy_session(path, project, "2025-01-01", "preserved")
    rows = path.read_bytes().splitlines()
    prefix = eol.join(rows[:1] if header_only else rows) + ending
    path.write_bytes(prefix)
    assert SessionStore.list(path.parent)[0]["messages"] == int(not header_only)
    session = make_session(session=path)
    assert path.read_bytes() == prefix and session.ui.lines == []
    store = session.store
    previous = None if header_only else "user"
    next_id = store.append_message(UserMessage("next"))
    move = store.append("leaf", target=previous)
    boundary = prefix if prefix.endswith(b"\n") else prefix + b"\n"
    raw = path.read_bytes()
    assert raw.startswith(boundary + b"{")
    saved = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert saved[-2]["parent_id"] == previous and saved[-1]["parent_id"] == next_id
    loaded = SessionStore.load(path)
    assert loaded.leaf == previous and loaded.entries[-1] == move
    assert [m.text() for m in loaded.build_context()[0]] == ([] if header_only else ["preserved"])


@pytest.mark.parametrize("outcome", ["completed", "repair-io-error"])
def test_tail_repair_revalidates_before_truncation(make_session, project, monkeypatch, outcome):
    path = Paths(project).sessions / "race.jsonl"
    write_legacy_session(path, project, "2025-01-01")
    record = b'{"type":"message","id":"late","parent_id":"user","message":{"role":"user","content":"late"}}\n'
    prefix = path.read_bytes()
    path.write_bytes(prefix + record[:20])
    original = builtins.open
    entered = []

    def on_writable_open(target, mode="r", *args, **kwargs):
        if target == path and mode == "r+b":
            entered.append(target)
            if outcome == "repair-io-error":
                raise OSError("injected repair failure")
            with path.open("ab") as writer:
                writer.write(record[20:])
        return original(target, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", on_writable_open)
    ui = RecordingUI()
    if outcome == "repair-io-error":
        with pytest.raises((ValueError, OSError), match="injected repair failure"):
            make_session(session=path, ui=ui).prompt("must not run")
        assert not ui.events and not calls() and path.read_bytes() == prefix + record[:20]
    else:
        session = make_session(session=path, ui=ui)
        assert session.store.leaf == SessionStore.load(path).leaf == "late"
        assert session.main.messages[-1].text() == "late" and path.read_bytes() == prefix + record
    assert entered == [path] and not ui.lines


def test_session_readers_can_overlap_across_processes(project):
    path = Paths(project).sessions / "readers.jsonl"
    write_legacy_session(path, project, "2025-01-01")
    with _locked_file(path, "rb"):
        subprocess.run(
            [sys.executable, "-c", "import sys; from virgo.harness.store import SessionStore; "
             "SessionStore.load(sys.argv[1])", str(path)],
            check=True, timeout=15,
        )


def _append_from_threads(path, writer, barrier):
    def append(thread):
        store = SessionStore.load(path)
        barrier.wait(timeout=15)
        for i in range(12):
            store.append("custom", kind="worker", data=[writer, thread, i])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, range(2)))


def test_concurrent_appends_share_one_boundary(project):
    path = Paths(project).sessions / "workers.jsonl"
    write_legacy_session(path, project, "2025-01-01")
    path.write_bytes(path.read_bytes().rstrip(b"\r\n"))
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    process = ctx.Process(target=_append_from_threads, args=(path, "child", barrier))
    process.start()
    try:
        _append_from_threads(path, "parent", barrier)
        process.join(15)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
    entries = SessionStore.load(path).entries
    records = [tuple(e["data"]) for e in entries if e["type"] == "custom"]
    assert sorted(records) == [(w, t, i) for w in ("child", "parent") for t in range(2) for i in range(12)]
    assert set(path.parent.iterdir()) == {path, path.parent / ".workspace.json"}
