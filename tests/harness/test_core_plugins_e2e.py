"""Acceptance boundaries fixed before the Core/plugin refactor.

Failure cases: disabled modules execute or start workers; minimal loses the real
coding loop; a late hook bypasses permission; conflicting/partial registrations
leak; schema publication changes an active batch; cleanup stops after one error;
an untrusted repository grants itself Python execution; children elevate rights,
lose cancellation or spend independent budgets; retired data is modified/replayed.
These scenarios use real sessions, dispatch, persistence and local processes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import TextContent, ToolResultMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.session import AgentSession
from virgo.harness.store import SessionStore

from tests.support.harness import RecordingUI, calls, tool_results
from tests.support.shell import selected


def test_minimal_coding_loop_and_resume_without_plugin_imports(make_session, project, python_command):
    session = make_session(settings={"profile": "minimal"}, persist=True, refresh_models=False)
    shell = selected(session)
    faux.script("test",
                faux.reply(tools=[("write", {"path": "answer.txt", "content": "before\n"})]),
                faux.reply(tools=[("edit", {"path": "answer.txt", "old_text": "before", "new_text": "after"})]),
                faux.reply(tools=[("read", {"path": "answer.txt"}),
                                  (shell.tool_name, {"command": python_command("print('verified')", shell=shell)})]),
                faux.reply("done"))
    session.prompt("Edit and verify the requested file")
    assert (project / "answer.txt").read_text() == "after\n"
    assert len(tool_results(session)) == 4 and not any(result.is_error for result in tool_results(session))
    assert set(calls()[0]["tools"]) == {"read", "write", "edit", "glob", "grep", shell.tool_name}
    assert all(request["system"] == calls()[0]["system"] for request in calls())
    restored = SessionStore.load(session.store.path)
    assert restored.build_context()[0] == session.main.messages
    script = textwrap.dedent("""
        import json, sys, threading
        from virgo.harness.session import AgentSession
        before = {t.ident for t in threading.enumerate()}
        session = AgentSession(sys.argv[1], model='faux/isolated', session=None,
                               settings={'profile': 'minimal'}, extensions=False,
                               refresh_models=False)
        session.prompt('hello')
        assert not any(name.startswith(('virgo.features.', 'virgo.plugins.')) for name in sys.modules)
        assert {t.ident for t in threading.enumerate()} == before
        assert len(session.main.tools) == 6
        session.shutdown()
        print('isolated')
    """)
    result = subprocess.run([sys.executable, "-c", script, str(project)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated" in result.stdout


def test_minimal_permission_rechecks_hook_rewritten_arguments(make_session, project):
    session = make_session(faux.reply(tools=[("write", {"path": "allowed.txt", "content": "x"})]),
                           faux.reply("done"), mode="auto-review", settings={"profile": "minimal",
                                                         "permissions": {"deny": ["write(secret.txt)"]}})
    ExtensionAPI(session, "rewrite").on("tool_call", lambda event, _: event.args.update(path="secret.txt"))
    session.prompt("write only allowed.txt")
    assert not (project / "secret.txt").exists()
    assert tool_results(session)[0].is_error


def test_owned_publication_preserves_the_running_batch(make_session):
    session = make_session(settings={"profile": "minimal"})
    api = ExtensionAPI(session, "publisher")
    replacement = AgentTool("snapshot", "new", {"type": "object"}, lambda args, ctx: "new", sequential=True)

    def old(args, ctx):
        api.replace_tools("dynamic", [(replacement, ("main",))])
        return "old"

    original = AgentTool("snapshot", "old", {"type": "object"}, old, sequential=True)
    api.replace_tools("dynamic", [(original, ("main",))])
    with pytest.raises(ValueError, match="(?i)(conflict|registered|owned|duplicate)"):
        ExtensionAPI(session, "conflict").register_tool(replacement)
    with pytest.raises(ValueError):
        api.replace_tools("dynamic", [(replacement, ("main",)), (session.tool("read"), ("main",))])
    assert session.tool("snapshot") is original
    faux.script("test", faux.reply(tools=[("snapshot", {}, "first"), ("snapshot", {}, "second")]),
                faux.reply(tools=[("snapshot", {}, "third")]), faux.reply("done"))
    session.prompt("check both generations")
    assert [result.text() for result in tool_results(session)] == ["old", "old", "new"]


def test_failed_plugin_rolls_back_and_cleanup_continues(make_session, project):
    home = project.parent / "home"
    directory = home / "extensions"
    directory.mkdir()
    marker = project / "cleanup.txt"
    (directory / "partial.py").write_text(textwrap.dedent(f"""
        from pathlib import Path
        from virgo.agent import AgentTool
        def setup(api):
            def clean():
                Path({str(marker)!r}).write_text('rollback')
            api.on_cleanup(clean)
            api.register_tool(AgentTool('partial_tool', 'partial', {{'type': 'object'}}, lambda a,c: 'bad'))
            api.register_command('partial_command', lambda s,a: None)
            api.on('input', lambda text, session: 'modified input')
            raise ValueError('partial setup failed')
    """), encoding="utf-8")
    session = make_session(faux.reply("done"), settings={"profile": "minimal"})
    assert session.tool("partial_tool") is None and session.commands.get("partial_command") is None
    assert marker.read_text() == "rollback"
    session.prompt("original input")
    assert calls()[0]["messages"][0]["content"] == "original input"
    assert "partial setup failed" in session.ui.text
    order = []
    api = ExtensionAPI(session, "cleanup")
    api.on_cleanup(lambda: order.append("first"))

    def broken():
        order.append("second")
        raise RuntimeError("cleanup failed")

    api.on_cleanup(broken)
    api.on_cleanup(lambda: order.append("third"))
    session.shutdown()
    session.shutdown()
    assert order == ["third", "second", "first"]


def test_project_extensions_require_trust_outside_the_project(make_session, project):
    directory = project / ".virgo" / "extensions"
    directory.mkdir(parents=True)
    marker = project / "executed.txt"
    (directory / "local.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    (project / ".virgo" / "settings.json").write_text(json.dumps({
        "extensions": {"trustedProjects": [str(project)]}, "profile": "minimal",
    }))
    session = make_session(refresh_models=False, trust_project_extensions=False)
    assert not marker.exists()
    assert "trust" in session.ui.text.lower()
    trusted = make_session(refresh_models=False, trust_project_extensions=True)
    assert marker.read_text() == "executed"
    assert trusted.main is not None


def test_children_share_budget_permission_ceiling_and_cancellation(make_session, project):
    session = make_session(settings={"profile": "minimal", "budget": {"maxModelCalls": 2}}, mode="plan")
    api = ExtensionAPI(session, "delegate")
    child = api.create_runtime("child", "subagent", parent=session.main, permission="accept-all")
    faux.script("test", faux.reply(tools=[("write", {"path": "forbidden.txt", "content": "x"})]),
                faux.reply("child done"), faux.reply("must not be called"))
    child.agent.prompt("write")
    assert not (project / "forbidden.txt").exists()
    assert session.services["permissions"].mode_for("child") == "plan"
    session.prompt("a third request would exceed the shared budget")
    assert len(calls()) == 2
    assert session.main.messages[-1].stop_reason == "error"
    assert "budget" in session.main.messages[-1].error_message.lower()
    child.agent.abort()
    assert not session.main.agent.signal.is_set()
    sibling = api.create_runtime("sibling", "subagent", parent=session.main, signal=threading.Event())
    session.abort()
    assert sibling.agent.signal.is_set()
    api.release_runtime(child)
    api.release_runtime(sibling)


def test_retired_data_stays_untouched_and_history_remains_readable(make_session, project):
    paths = [project / ".virgo" / "schedules.json", project / ".virgo" / "tasks" / "old.json",
             project / ".virgo" / "runs" / "old.jsonl", project / ".virgo" / "memory" / "MEMORY.md"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legacy bytes\n")
    session = make_session(settings={"profile": "coding"}, persist=True, refresh_models=False)
    session.store.append_message(ToolResultMessage("old", "schedule", [TextContent("historical result")]))
    history = SessionStore.load(session.store.path).build_context()[0]
    assert history[-1].tool_name == "schedule"
    for name in ("schedule", "workflow", "team_spawn", "task_create", "memory", "goal"):
        assert session.tool(name) is None
    session.prompt("/schedule")
    assert "removed" in session.ui.text.lower() or "no longer" in session.ui.text.lower()
    session.shutdown()
    assert all(path.read_text() == "legacy bytes\n" for path in paths)


def test_profile_layers_and_explicit_plugin_overrides(make_session, project):
    home = project.parent / "home"
    (home / "settings.json").write_text(json.dumps({"plugins": {"todo": {"enabled": False}}}))
    (project / ".virgo").mkdir()
    (project / ".virgo" / "settings.json").write_text(json.dumps({"features": {"todo": True}}))
    session = make_session(settings={"profile": "minimal"})
    assert session.tool("todo_write") is not None
    disabled = make_session(settings={"profile": "coding", "plugins": {"todo": {"enabled": False},
                                                                         "web": {"enabled": False}}})
    assert disabled.tool("todo_write") is None and disabled.tool("web_search") is None
    assert "mcp" not in disabled.services
    with pytest.raises(ValueError, match="(?i)(unknown|plugin)"):
        AgentSession(project, model="faux/test", session=None, settings={"plugins": {"typo": {"enabled": True}}},
                     refresh_models=False, ui=RecordingUI())
    result = subprocess.run([sys.executable, "-m", "virgo", "--help"], env=os.environ.copy(),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and "--profile" in result.stdout and "--no-extensions" in result.stdout
