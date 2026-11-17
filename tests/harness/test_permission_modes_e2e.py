"""Execution profiles and one-operation exceptions through the existing Agent Loop."""
from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import AgentSession
from virgo.harness.settings import Settings

from tests.support.harness import RecordingUI, calls, tool_results


def request(tool="write", arguments=None, **flags):
    return ("request_permissions", {"tool": tool, "arguments": arguments or {"path": "result.txt", "content": "ok"},
                                    "permissions": flags, "reason": "Finish the requested operation"})


def test_explicit_permission_request_is_exact_once_and_does_not_change_mode(broker_session):
    args = {"path": "result.txt", "content": "ok"}
    session = broker_session(
        faux.reply(tools=[("write", args)]),
        faux.reply(tools=[request(write_paths=["result.txt"])]),
        faux.reply(tools=[("write", args)]),
        faux.reply(tools=[("write", args)]), faux.reply("done"),
        ui=RecordingUI(["y"]), preset="read-only",
    )
    session.prompt("Write the requested file after my permission")
    results = tool_results(session)
    assert [result.is_error for result in results] == [True, False, False, True]
    assert (session.cwd / "result.txt").read_text() == "ok"
    assert not session.services["execution"].mode_for(session.main).write_paths
    assert len(session.ui.questions) == 1
    assert results[0].details["boundary"]["dimension"] == "write_paths"
    assert "request_permissions" in calls()[0]["tools"]
    assert "write" in calls()[0]["tools"]


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
    assert not session.services["execution"]._pending


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

    monkeypatch.setattr(session.ui, "confirm_once", confirm)
    session.prompt("Request extra permission")
    assert tool_results(session)[0].is_error
    assert "expired" in tool_results(session)[0].text()
    assert not manager.mode_for(session.main).write_paths
    assert manager.selected_mode.write_paths


def test_offline_profile_blocks_web_and_mcp_before_dispatch(broker_session):
    session = broker_session(faux.reply(tools=[("web_fetch", {"url": "https://example.test"})]), faux.reply("done"),
                           network=False)
    session.prompt("Read a public page")
    result = tool_results(session)[0]
    assert result.is_error and result.details["boundary"]["dimension"] == "network_allowed"
    assert result.details["execution"]["started"] is False


def test_custom_mode_names_and_permissions_persist_without_changing_active_run(broker_session):
    session = broker_session()
    manager = session.services["execution"]
    manager.save_mode("offline", "Offline inspection", ["."], [], False)
    manager.select_mode("offline")
    assert manager.selected_mode.name == "Offline inspection"
    assert manager.mode_for(session.main).flags() == {"read_paths": [str(session.cwd)], "write_paths": [], "network_allowed": False}
    restored = Settings.load(session.paths)
    assert restored.get("permissions.presets.offline.name") == "Offline inspection"
    assert restored.get("permissions.preset") == "workspace-edit"
    manager.save_mode("read-only", "My read-only", ["."], ["."], True)
    assert manager.modes["read-only"].write_paths


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


def test_default_profile_is_host_without_resource_approval(project):
    session = AgentSession(project, model="faux/test", session=None, extensions=False, refresh_models=False)
    try:
        assert session.services["execution"].name == "host"
        assert "request_permissions" not in {t.name for t in session.main.tools}
    finally:
        session.shutdown()


@pytest.mark.parametrize("legacy", ["workspace-write", "read-only", "disabled"])
def test_legacy_native_policy_requires_explicit_migration(broker_session, legacy):
    with pytest.raises(ValueError, match="migrate"):
        broker_session(settings={"sandbox": {"mode": legacy}})
