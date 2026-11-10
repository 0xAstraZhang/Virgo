"""Harness: persistence, append-only requests, commands, hooks, extensions, notifications."""

from __future__ import annotations

import json
import shutil
import sys
import textwrap

from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, ToolResultMessage
from virgo.harness.session import AgentSession
from virgo.harness.settings import Paths
from virgo.harness.store import SessionStore

from .conftest import RecordingUI, calls


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
    s2 = AgentSession(project, ui=RecordingUI(), model="faux/test", session=path, permission_mode="yolo")
    assert [type(m).__name__ for m in s2.main.messages] == [type(m).__name__ for m in s.main.messages]
    s2.prompt("again")
    assert s2.main.agent.last_text == "second answer"
    assert SessionStore.list(path.parent)[0]["title"] == "hello"
    s2.shutdown()


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


def test_command_hooks(make_session, project):
    hook = project / "guard.py"
    hook.write_text(
        textwrap.dedent("""
        import json, sys
        data = json.load(sys.stdin)
        if "forbidden" in data["tool_input"].get("command", ""):
            print("no forbidden commands", file=sys.stderr)
            sys.exit(2)
    """)
    )
    settings = {"hooks": {"PreToolUse": [{"matcher": "bash", "command": f"{sys.executable} {hook}"}]}}
    s = make_session(
        faux.reply(tools=[("bash", {"command": "echo forbidden"}), ("bash", {"command": "echo fine"})]),
        faux.reply("ok"),
        settings=settings,
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


def test_retry_on_retryable_errors(make_session):
    ui = RecordingUI()
    s = make_session(
        faux.error("overloaded_error 529", kind="retryable"),
        faux.reply("recovered"),
        ui=ui,
        settings={"retry": {"maxRetries": 2}},
    )
    import virgo.harness.session as sess

    orig = sess.random.random
    sess.random.random = lambda: 0.0
    try:
        s.prompt("go")
    finally:
        sess.random.random = orig
    assert s.main.agent.last_text == "recovered"
    assert "retrying" in ui.text


def test_system_prompt_includes_context_files(make_session, project):
    (project / "AGENTS.md").write_text("Always use tabs.")
    s = make_session(faux.reply("ok"))
    s.prompt("hi")
    system = calls()[0]["system"]
    assert "Always use tabs." in system and "<tools>" in system and str(project) in system


def test_model_errors_are_reported(make_session):
    s = make_session(faux.error("bad request"))
    s.prompt("go")
    last = s.main.messages[-1]
    assert (
        isinstance(last, AssistantMessage)
        and last.stop_reason == "error"
        and last.error_message == "bad request"
    )


def test_examples_directory_loads(make_session):
    from pathlib import Path

    examples = Path(__file__).resolve().parents[1] / "examples"
    ui = RecordingUI()
    s = make_session(faux.reply("ok"), cwd=examples, ui=ui)
    assert "stats" in s.commands.names() and "review" in s.templates
    assert "code-review" in s.services["skills"] and "reviewer" in s.services["subagents"].types
    s.prompt("hi")
    system = calls()[0]["system"]
    assert "review-changes" in system and "code-review" in system
    assert not [line for line in ui.lines if "failed" in line]
