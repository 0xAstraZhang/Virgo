"""Execution profiles and one-operation exceptions through the existing Agent Loop."""
from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.cli.console import ConsoleBroker, TerminalUI
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI, calls, tool_results


def request(tool="write", arguments=None, **flags):
    return ("request_permissions", {"tool": tool, "arguments": arguments or {"path": "result.txt", "content": "ok"},
                                    "permissions": flags, "reason": "Finish the requested operation"})


def test_explicit_permission_request_persists_in_session_and_does_not_change_mode(broker_session):
    args = {"path": "result.txt", "content": "ok"}
    updated = {"path": "result.txt", "content": "updated"}
    session = broker_session(
        faux.reply(tools=[request(write_paths=["result.txt"]), ("write", args)]),
        faux.reply(tools=[("write", updated)]),
        faux.reply(tools=[("write", {"path": "unapproved.txt", "content": "no"})]),
        faux.reply("done"),
        ui=RecordingUI(["y", "n"]), preset="read-only",
    )
    session.prompt("Write the requested file after my permission")
    results = tool_results(session)
    assert [result.is_error for result in results] == [False, False, False, True]
    assert (session.cwd / "result.txt").read_text() == "updated"
    assert not (session.cwd / "unapproved.txt").exists()
    assert not session.services["execution"].mode_for(session.main).write_paths
    assert len(session.ui.questions) == 2
    assert results[-1].details["boundary"]["dimension"] == "write_paths"
    assert "request_permissions" in calls()[0]["tools"]
    assert "write" in calls()[0]["tools"]


@pytest.mark.parametrize("ui_adapter", ["recording", "terminal"])
def test_direct_file_approval_persists_in_session_and_preserves_the_mode(
    broker_session, tmp_path, monkeypatch, ui_adapter,
):
    if ui_adapter == "recording":
        ui = RecordingUI(["y", "n"])
        questions = ui.questions
    else:
        broker = ConsoleBroker()
        ui = TerminalUI(broker)
        questions = []
        answers = iter(["y", "n"])

        def read_value(question, write):
            questions.append(question)
            return next(answers)

        monkeypatch.setattr(broker, "read_value", read_value)
    target = tmp_path / "result.txt"
    other = tmp_path / "other.txt"
    session = broker_session(
        faux.reply(tools=[("write", {"path": str(target), "content": "one"})]),
        faux.reply(tools=[("write", {"path": str(target), "content": "two"})]),
        faux.reply(tools=[("write", {"path": str(other), "content": "blocked"})]),
        faux.reply("done"),
        preset="read-only",
        ui=ui,
    )
    session.prompt("Write the requested result")
    assert [result.is_error for result in tool_results(session)] == [False, False, True]
    assert target.read_text() == "two"
    assert not other.exists()
    assert session.main.agent.last_text == "done"
    assert len(questions) == 2
    for question, path in zip(questions, (target, other), strict=True):
        assert f"Write access: {path.resolve()}" in question.removeprefix("? ").splitlines()
        assert str(session.cwd) in question and "Reason:" in question
    assert session.main.permission_snapshot.mode.id == "read-only"


def test_changed_arguments_cannot_consume_another_path_permission(broker_session):
    session = broker_session(
        faux.reply(tools=[request(write_paths=["result.txt"])]),
        faux.reply(tools=[("write", {"path": "other.txt", "content": "ok"})]),
        faux.reply(tools=[("write", {"path": "result.txt", "content": "ok"})]), faux.reply("done"),
        ui=RecordingUI(["y", "n"]), preset="read-only",
    )
    session.prompt("Write only the requested file")
    assert [result.is_error for result in tool_results(session)] == [False, True, False]
    assert not (session.cwd / "other.txt").exists()
    assert (session.cwd / "result.txt").read_text() == "ok"


def test_permission_request_cannot_grant_a_whole_child_runtime(broker_session):
    session = broker_session(
        faux.reply(tools=[request("agent", {"description": "Implement", "prompt": "Write a file"},
                                  write_paths=["result.txt"])]), faux.reply("done"),
        ui=RecordingUI(["y"]), preset="read-only",
    )
    session.prompt("Request an operation exception")
    result = tool_results(session)[0]
    assert result.is_error
    assert "not a child task" in result.text()
    assert not session.ui.questions
    assert not session.services["execution"].sandbox._session_grants


def test_deny_and_noninteractive_policy_prevent_escalation(broker_session):
    session = broker_session(faux.reply(tools=[request(write_paths=["result.txt"])]), faux.reply("done"),
                             approval="never", ui=RecordingUI(["y"]), preset="read-only")
    session.prompt("Request extra permission")
    assert tool_results(session)[0].is_error
    assert not session.ui.questions


def test_permission_change_during_confirmation_expires_request(broker_session, monkeypatch):
    session = broker_session(faux.reply(tools=[request(write_paths=["result.txt"])]), faux.reply("done"),
                           preset="read-only")
    manager = session.services["execution"]

    def confirm(*_):
        manager.select_mode("workspace-edit")
        return "y"

    monkeypatch.setattr(session.ui, "confirm", confirm)
    session.prompt("Request extra permission")
    assert not tool_results(session)[0].is_error
    assert not manager.mode_for(session.main).write_paths
    assert manager.selected_mode.write_paths


def test_read_only_preset_allows_web_fetch_while_keeping_shell_network_off(broker_session):
    from virgo.agent import ToolCallEvent
    session = broker_session(preset="read-only")
    tool = session.tool("web_fetch")
    event = ToolCallEvent("web", tool.name, {"url": "https://example.test"}, tool, "main",
                          session.cwd, origin=session.main.origin)
    assert session._before_tool(event) is None
    assert not session.main.permission_snapshot.mode.network and not session.ui.questions


def test_custom_mode_names_and_permissions_persist_without_changing_active_run(broker_session):
    session = broker_session()
    manager = session.services["execution"]
    assert list(manager.modes) == ["read-only", "workspace-edit", "no-limit"]
    manager.select_mode("read-only")
    flags = manager.mode_for(session.main).flags()
    assert flags["read_paths"] == [str(session.cwd)]
    assert flags["write_paths"] == []
    assert flags["network_allowed"] is False
    assert flags["web_allowed"] is True
    assert flags["unrestricted"] is False
    assert str(session.paths.user.resolve()) in flags["read_deny_paths"]
    manager.select_mode("no-limit")
    assert session.main.execution_context.provider == "host"
    manager.select_mode("workspace-edit")
    assert session.main.permission_snapshot.mode == manager.modes["workspace-edit"]


def test_inactive_conversation_keeps_its_policy_when_another_selection_changes(broker_session):
    session = broker_session(persist=True)
    manager = session.services["execution"]
    manager.select_mode("read-only")
    previous = session.main
    session.new_session()
    manager.select_mode("workspace-edit")
    previous.prepare_run()
    assert not manager.mode_for(previous).write_paths
    assert manager.mode_for(session.main).write_paths


def test_child_created_before_mode_change_keeps_its_origin_permissions(broker_session):
    session = broker_session()
    manager = session.services["execution"]
    manager.select_mode("read-only")
    child = session.create_runtime("queued-child", "subagent", parent=session.main)
    try:
        manager.select_mode("workspace-edit")
        faux.push("test", faux.reply(tools=[("write", {"path": "child.txt", "content": "bad"})]),
                  faux.reply("done"))
        child.agent.prompt("Inspect the workspace")
        assert not manager.mode_for(child).write_paths
        assert not (session.cwd / "child.txt").exists()
    finally:
        session.release_runtime(child)


def test_default_profile_is_workspace_edit_with_resource_approval(project):
    from virgo.execution import default_execution_mode

    expected = default_execution_mode()
    session = AgentSession(project, model="faux/test", session=None, extensions=False, refresh_models=False)
    try:
        assert session.services["execution"].selected_mode_id == expected
        if expected == "no-limit":
            assert session.services["execution"].name == "host"
            assert "request_permissions" not in {t.name for t in session.main.tools}
        else:
            assert session.services["execution"].name == "sandbox"
            assert "request_permissions" in {t.name for t in session.main.tools}
    finally:
        session.shutdown()


def test_explicit_no_limit_profile_is_host_without_resource_approval(project):
    session = AgentSession(project, model="faux/test", session=None, extensions=False, refresh_models=False,
                           settings={"execution": {"mode": "no-limit"}})
    try:
        assert session.services["execution"].name == "host"
        assert "request_permissions" not in {t.name for t in session.main.tools}
    finally:
        session.shutdown()


def test_no_limit_sandbox_switch_preserves_the_active_host_and_child(broker_session):
    session = broker_session(preset="no-limit")
    router = session.services["execution"]
    assert router.sandbox is None
    main = session.main
    main.prepare_run()
    child = session.create_runtime("host-worker", "subagent", parent=main)
    try:
        router.select_mode("read-only")
        assert main.execution_context.provider == "host"
        assert router.selected_mode_id == "read-only"
        child.prepare_run()
        assert child.execution_context.provider == "host"
        main.prepare_run()
        main.refresh_tools()
        assert "request_permissions" in [t.name for t in main.request_context([]).tools]
        assert main.permission_snapshot.mode.id == "read-only"
        router.select_mode("no-limit")
        main.prepare_run()
        assert main.execution_context.provider == "host"
        assert "request_permissions" not in [t.name for t in main.request_context([]).tools]
    finally:
        session.release_runtime(child)


@pytest.mark.parametrize("legacy", ["workspace-write", "read-only", "disabled"])
def test_legacy_native_policy_requires_explicit_migration(make_session, legacy):
    """S7-02: All legacy sandbox.mode settings (including disabled) are rejected explicitly."""
    settings = {"sandbox": {"mode": legacy}}
    with pytest.raises(ValueError, match="execution\\.mode"):
        make_session(settings=settings)


@pytest.mark.parametrize("operation", ["select", "save"])
def test_failed_preset_change_preserves_effective_and_saved_state(broker_session, operation):
    session = broker_session()
    provider = session.services["execution"]
    active = session.main.execution_context
    revision = session.main.origin.root.preset_revision
    settings_file = session.paths.user / "settings.json"
    previous = settings_file.read_bytes() if settings_file.exists() else None
    with pytest.raises(ValueError, match="Choose"):
        provider.command(session, "subdir" if operation == "select" else "save workspace-edit {}")
    assert provider.selected_mode_id == "workspace-edit" and session.main.origin.root.preset_revision == revision
    assert session.main.execution_context is active
    assert (settings_file.read_bytes() if settings_file.exists() else None) == previous
    faux.push("test", faux.reply(tools=[("read", {"path": "note.txt"})]), faux.reply("done"))
    (session.cwd / "note.txt").write_text("ready")
    session.prompt("Read note.txt")
    assert not tool_results(session)[0].is_error


def test_child_approval_ceiling_survives_nested_creation_and_new_runs(broker_session):
    session = broker_session(ui=RecordingUI(["n"]), preset="read-only", approval="ask")
    child = session.create_runtime("child", "subagent", parent=session.main, approval_policy="never")
    grandchild = session.create_runtime("grandchild", "subagent", parent=child, approval_policy="ask")
    try:
        for runtime in (child, grandchild):
            faux.push("test", faux.reply(tools=[request(write_paths=["result.txt"])]), faux.reply("done"))
            runtime.agent.prompt("Request permission for the next operation")
            assert runtime.permission_snapshot.approval == "never"
            assert not session.ui.questions
    finally:
        session.release_runtime(grandchild)
        session.release_runtime(child)


def test_queued_child_uses_submitting_runs_approval_and_resources(broker_session, monkeypatch):
    import threading

    from virgo.agent.types import ToolContext
    from virgo.plugins.subagent import Subagents

    session = broker_session(ui=RecordingUI(["n"]), preset="read-only", approval="never")
    owner = session.main
    release, waiting = threading.Event(), threading.Event()
    run = Subagents.run
    captured = []
    session.bus.on("runtime_created", lambda rt: captured.append(rt) if rt.role == "subagent" else None)

    def delayed(self, *args, **kwargs):
        waiting.set()
        assert release.wait(10)
        return run(self, *args, **kwargs)

    monkeypatch.setattr(Subagents, "run", delayed)
    ctx = ToolContext("queued", session.cwd, threading.Event(), origin=owner.origin)
    session.tool("agent").execute({"description": "inspect", "prompt": "inspect", "background": True}, ctx)
    job = next(iter(session.services["jobs"].jobs.values()))
    try:
        assert waiting.wait(10)
        session.approval_policy = "ask"
        session.services["execution"].select_mode("workspace-edit")
        owner.refresh_permission_snapshot()
        session.new_session()
        faux.push("test", faux.reply(tools=[request(write_paths=["result.txt"])]), faux.reply("done"))
        release.set()
        assert job.done.wait(10)
        child, = captured
        assert child.origin.root is owner.origin.root
        assert not child.execution_context.resources.write_paths
        assert child.permission_snapshot.approval == "never"
        assert not session.ui.questions
    finally:
        release.set()
        job.done.wait(10)


def test_next_preset_selected_after_admission_applies_to_next_task(broker_session, monkeypatch):
    import json
    from contextlib import contextmanager

    session = broker_session(preset="read-only")
    provider = session.services["execution"]

    @contextmanager
    def admitted():
        provider.select_mode("workspace-edit")
        provider.status(session, "")
        yield

    monkeypatch.setattr(session.ui, "command_suspended", admitted)
    faux.push("test", faux.reply("done"))
    session.prompt("Inspect the workspace")
    status = json.loads(session.ui.lines[-1].split("\n", 1)[1])
    assert status["current_mode"] == "Read-Only" and status["shell_network"] is False
    assert status["next_mode"] == "Workspace-Edit" and status["web_allowed"] is True
    assert session.main.permission_snapshot.mode.id == "read-only"
    faux.push("test", faux.reply("done"))
    session.prompt("Continue with the selected preset")
    assert session.main.permission_snapshot.mode.id == "workspace-edit"


@pytest.mark.parametrize(("action", "change_at", "allowed"), [
    ("workspace-edit", "confirmation", True), ("workspace-edit", "dispatch", True),
    ("revoke", "confirmation", False), ("never", "dispatch", False),
])
def test_child_grant_respects_scope_of_changes_in_other_conversation(broker_session, monkeypatch, action, change_at, allowed):
    session = broker_session(ui=RecordingUI(["y"]), preset="read-only")
    child = session.create_runtime("worker", "subagent", parent=session.main)
    args = {"path": "result.txt", "content": "ok"}
    session.new_session()
    provider = session.services["execution"]

    def switch(*_):
        provider.command(session, action)
        return "y"

    def dispatch(_):
        if change_at == "dispatch":
            switch()
        return faux.reply(tools=[("write", args)])

    if change_at == "confirmation":
        monkeypatch.setattr(session.ui, "confirm", switch)
    try:
        faux.push("test", faux.reply(tools=[request(write_paths=["result.txt"])]), dispatch, faux.reply("done"))
        child.agent.prompt("Create the approved file")
        assert child.permission_snapshot.mode.id == "read-only"
        target = session.cwd / "result.txt"
        assert (target.read_text() if target.exists() else None) == ("ok" if allowed else None)
    finally:
        session.release_runtime(child)


def test_request_permissions_hidden_when_escalation_disabled(broker_session, project):
    from virgo.harness.ui import UI

    # Interactive sandbox session exposes request_permissions and guidance.
    interactive = broker_session(preset="read-only", approval="ask")
    ctx = interactive.main.request_context([])
    assert "request_permissions" in [t.name for t in ctx.tools]
    assert "Use request_permissions" in ctx.system

    # approval="never" hides request_permissions and updates boundary guidance.
    never = broker_session(preset="read-only", approval="never")
    never_ctx = never.main.request_context([])
    assert "request_permissions" not in [t.name for t in never_ctx.tools]
    assert "Use request_permissions" not in never_ctx.system
    assert "escalation is disabled" in never_ctx.system

    # Headless UI without confirm() also hides request_permissions.
    headless = broker_session(preset="read-only", approval="ask", ui=UI())
    headless_ctx = headless.main.request_context([])
    assert "request_permissions" not in [t.name for t in headless_ctx.tools]
    assert "Use request_permissions" not in headless_ctx.system


def test_execution_scope_validation_and_sandbox_layering():
    import ast
    from pathlib import Path

    from virgo.agent.types import EXECUTION_SCOPES, AgentTool
    from virgo.tools import EDIT, GLOB, GREP, READ, WRITE
    from virgo.tools.shell import Shell, make_shell_tool

    assert "network" not in EXECUTION_SCOPES
    for file_tool in (READ, WRITE, EDIT, GLOB, GREP):
        assert file_tool.execution_scope == "file"
    assert make_shell_tool(Shell("bash", "/bin/bash", "5.0")).execution_scope == "process"
    with pytest.raises(ValueError, match="execution_scope"):
        AgentTool("bad", "desc", {"type": "object"}, lambda *_: "ok", execution_scope="network")

    # virgo.sandbox must stay free of reverse imports from virgo.harness.
    sandbox_dir = Path(__file__).resolve().parents[2] / "src" / "virgo" / "sandbox"
    for path in sandbox_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "harness" in node.module:
                raise AssertionError(f"Reverse harness import in {path.name}: {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "harness" not in alias.name, f"Reverse harness import in {path.name}: {alias.name}"


def test_builtin_tool_localization_covers_read_output_and_request_permissions(broker_session):
    from virgo.i18n import Translator

    session = broker_session(preset="read-only")
    zh = Translator("zh-CN")
    en = Translator("en")

    read_output = session.tool("read_output")
    req_perm = session.tool("request_permissions")
    assert read_output is not None and req_perm is not None
    assert read_output.spec(zh).description != read_output.spec(en).description
    assert "output_id" in read_output.spec(zh).description
    assert req_perm.spec(zh).description != req_perm.spec(en).description
    assert "权限" in req_perm.spec(zh).description
    shell = session.services["shell"].current
    assert shell is not None
    shell_tool = session.tool(shell.tool_name)
    assert shell_tool is not None
    assert "read_output" in shell_tool.spec(en).description
    assert "read_output" in shell_tool.spec(zh).description
    assert "sed -n" not in en("tools.fs.long_line", line=1, max_kb=50, path="a.txt")
    assert "sed -n" not in zh("tools.fs.long_line", line=1, max_kb=50, path="a.txt")


def test_status_shows_session_and_granted_write_path(broker_session, tmp_path):
    target = tmp_path / "approved.txt"
    session = broker_session(
        faux.reply(tools=[("write", {"path": str(target), "content": "ok"})]),
        faux.reply("done"), preset="read-only", ui=RecordingUI(["y"]),
    )
    session.ui.interactive = True
    session.prompt("Write the file")
    assert session.commands.get("status").concurrent
    session.prompt("/status")
    output = session.ui.lines[-1]
    assert session.store.id in output and session.main.model.spec in output
    assert f"Read roots: {session.cwd}" in output
    assert f"Write roots: {target}" in output
    assert "Read-Only" in output and "Shell network: off" in output
    assert "Token usage:" in output and "Context:" in output
    session.prompt("/permissions")
    summary = session.ui.lines[-1]
    assert f"Write roots: {target}" in summary
    assert summary in output and session.store.id not in summary


@pytest.mark.parametrize(("language", "expected"), [
    ("en", ("Permissions: Read-Only · approval: never", "Next task: Workspace-Edit",
            "Write roots: none", "Web: on · Shell network: off")),
    ("zh-CN", ("权限：Read-Only · 审批策略：never", "下一任务：Workspace-Edit", "可写路径：无",
               "受保护路径与例外规则：/permissions show", "网页访问：开 · Shell 联网：关")),
])
def test_permissions_summary_during_task_keeps_active_approval_and_mode(broker_session, language, expected):
    def during_task(_):
        session.prompt("/permissions ask")
        session.prompt("/permissions workspace-edit")
        session.prompt("/permissions")
        return faux.reply("done")

    session = broker_session(during_task, preset="read-only", approval="never", settings={"language": language})
    session.ui.interactive = True
    session.prompt("Inspect the workspace")
    for row in expected:
        assert row in session.ui.lines[-1].splitlines()


def test_status_reports_host_authority_without_sandbox_roots(make_session):
    session = make_session(settings={"execution": {"mode": "no-limit"}}, ui=RecordingUI())
    session.prompt("/status")
    output = session.ui.lines[-1]
    assert "Permissions: No-Limit" in output and "Host · OS permissions" in output
    assert "Read roots:" not in output and "Next task:" not in output
