"""Execution profiles and one-operation exceptions through the existing Agent Loop."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.execution import ExecutionUnavailable, FileAccessDenied
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI, calls, tool_results


def request(tool="write", arguments=None, **flags):
    return ("request_permissions", {"tool": tool, "arguments": arguments or {"path": "result.txt", "content": "ok"},
                                    "permissions": flags, "reason": "Finish the requested operation"})


def test_explicit_permission_request_is_exact_once_and_does_not_change_mode(broker_session):
    args = {"path": "result.txt", "content": "ok"}
    session = broker_session(
        faux.reply(tools=[request(write_paths=["result.txt"])]),
        faux.reply(tools=[("write", args)]),
        faux.reply(tools=[("write", args)]), faux.reply("done"),
        ui=RecordingUI(["y"]), preset="read-only",
    )
    session.prompt("Write the requested file after my permission")
    results = tool_results(session)
    assert [result.is_error for result in results] == [False, False, True]
    assert (session.cwd / "result.txt").read_text() == "ok"
    assert not session.services["execution"].mode_for(session.main).write_paths
    assert len(session.ui.questions) == 2
    assert results[-1].details["boundary"]["dimension"] == "write_paths"
    assert "request_permissions" in calls()[0]["tools"]
    assert "write" in calls()[0]["tools"]


def test_direct_file_approval_dispatches_once_and_preserves_the_mode(broker_session, tmp_path):
    target = tmp_path / "result.txt"
    args = {"path": str(target), "content": "one"}
    session = broker_session(faux.reply(tools=[("write", args)]), faux.reply(tools=[("write", args)]),
                             faux.reply("done"), preset="read-only", ui=RecordingUI(["y", "n"]))
    session.prompt("Write the requested result")
    assert [result.is_error for result in tool_results(session)] == [False, True]
    assert target.read_text() == "one"
    assert len(session.ui.questions) == 2
    assert all(str(session.cwd) in question and "write_paths" in question and "Reason:" in question
               for question in session.ui.questions)
    assert session.main.permission_snapshot.mode.id == "read-only"


def test_changed_arguments_cannot_consume_another_operation_permission(broker_session):
    session = broker_session(
        faux.reply(tools=[request(write_paths=["result.txt"])]),
        faux.reply(tools=[("write", {"path": "other.txt", "content": "ok"})]),
        faux.reply(tools=[("write", {"path": "result.txt", "content": "ok"})]), faux.reply("done"),
        ui=RecordingUI(["y"]), preset="read-only",
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
    assert not session.services["execution"].sandbox._pending


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


def test_offline_profile_blocks_web_and_mcp_before_dispatch(broker_session):
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
    assert manager.mode_for(session.main).flags() == {
        "read_paths": [str(session.cwd)], "write_paths": [], "network_allowed": False, "web_allowed": True}
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
    session = AgentSession(project, model="faux/test", session=None, extensions=False, refresh_models=False)
    try:
        assert session.services["execution"].name == "sandbox"
        assert session.services["execution"].selected_mode_id == "workspace-edit"
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


# Keep the historical test identity until the retirement comparison has a
# baseline that contains the renamed test. It now exercises explicit No-Limit.
test_default_profile_is_host_without_resource_approval = (
    test_explicit_no_limit_profile_is_host_without_resource_approval
)


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
    settings = {"sandbox": {"mode": legacy}}
    if legacy == "disabled":
        session = make_session(settings=settings)
        assert session.services["execution"].selected_mode_id == "no-limit"
        assert session.services["execution"].sandbox is None
    else:
        with pytest.raises(ValueError, match="migrate"):
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
    status = json.loads(session.ui.lines[0].split("\n", 1)[1])
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


@pytest.mark.parametrize("preset", ["workspace-edit", "read-only"])
def test_workspace_may_contain_the_private_state_directory(broker_session, tmp_path, preset):
    home = Path(os.environ["VIRGO_HOME"])
    (home / "secret.txt").write_text("token")
    (tmp_path / "notes.txt").write_text("visible")
    session = broker_session(cwd=tmp_path, preset=preset)
    contract = session.main.execution_context
    assert contract.validate(tmp_path) == tmp_path.resolve()
    assert contract.read_bytes(tmp_path / "notes.txt") == b"visible"
    with pytest.raises(FileAccessDenied):
        contract.read_bytes(home / "secret.txt")
    root, parts = contract.spool_location()
    nested = root.joinpath(*parts, "subdir")
    nested.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ExecutionUnavailable):
        contract.validate(home)
    if nested.is_relative_to(home):
        with pytest.raises(ExecutionUnavailable):
            contract.validate(nested)
