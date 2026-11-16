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


def test_explicit_permission_request_is_exact_once_and_does_not_change_mode(make_session):
    args = {"path": "result.txt", "content": "ok"}
    session = make_session(
        faux.reply(tools=[("write", args)]),
        faux.reply(tools=[request(read_only=False)]),
        faux.reply(tools=[("write", args)]),
        faux.reply(tools=[("write", args)]), faux.reply("done"),
        ui=RecordingUI(["y"]), settings={"sandbox": {"mode": "read-only"}},
    )
    session.prompt("Write the requested file after my permission")
    results = tool_results(session)
    assert [result.is_error for result in results] == [True, False, False, True]
    assert (session.cwd / "result.txt").read_text() == "ok"
    assert session.services["sandbox"].mode_for(session.main).read_only
    assert len(session.ui.questions) == 1
    assert '"dimension":"read_only"' in results[0].text()
    assert "request_permissions" in calls()[0]["tools"]
    assert "write" in calls()[0]["tools"]


def test_changed_arguments_cannot_consume_another_operation_permission(make_session):
    session = make_session(
        faux.reply(tools=[request(read_only=False)]),
        faux.reply(tools=[("write", {"path": "other.txt", "content": "ok"})]),
        faux.reply(tools=[("write", {"path": "result.txt", "content": "ok"})]), faux.reply("done"),
        ui=RecordingUI(["y"]), settings={"sandbox": {"mode": "read-only"}},
    )
    session.prompt("Write only the requested file")
    assert [result.is_error for result in tool_results(session)] == [False, True, False]
    assert not (session.cwd / "other.txt").exists()
    assert (session.cwd / "result.txt").read_text() == "ok"


def test_permission_request_cannot_grant_a_whole_child_runtime(make_session):
    session = make_session(
        faux.reply(tools=[request("agent", {"description": "Implement", "prompt": "Write a file"},
                                  read_only=False)]), faux.reply("done"),
        ui=RecordingUI(["y"]), settings={"sandbox": {"mode": "read-only"}},
    )
    session.prompt("Request an operation exception")
    result = tool_results(session)[0]
    assert result.is_error
    assert "managed operation" in result.text()
    assert not session.ui.questions
    assert not session.services["sandbox"]._pending


def test_deny_and_noninteractive_policy_prevent_escalation(make_session):
    for settings in ({"permissions": {"deny": ["write(*)"]}}, {"permissions": {"approval": "never"}}):
        session = make_session(faux.reply(tools=[request(read_only=False)]), faux.reply("done"),
                               approval=settings["permissions"].get("approval", "ask"), ui=RecordingUI(["y"]),
                               settings={**settings, "sandbox": {"mode": "read-only"}})
        session.prompt("Request extra permission")
        assert tool_results(session)[0].is_error
        assert not session.ui.questions


def test_permission_change_during_confirmation_expires_request(make_session, monkeypatch):
    session = make_session(faux.reply(tools=[request(read_only=False)]), faux.reply("done"),
                           settings={"sandbox": {"mode": "read-only"}})
    manager = session.services["sandbox"]

    def confirm(*_):
        manager.select_mode("workspace-edit")
        return "y"

    monkeypatch.setattr(session.ui, "confirm_once", confirm)
    session.prompt("Request extra permission")
    assert tool_results(session)[0].is_error
    assert "expired" in tool_results(session)[0].text()
    assert manager.mode_for(session.main).read_only
    assert not manager.selected_mode.read_only


def test_offline_profile_blocks_web_and_mcp_before_dispatch(make_session):
    session = make_session(faux.reply(tools=[("web_fetch", {"url": "https://example.test"})]), faux.reply("done"),
                           settings={"sandbox": {"mode": "workspace-write"}})
    session.prompt("Read a public page")
    result = tool_results(session)[0]
    assert result.is_error and '"dimension":"network_allowed"' in result.text()
    assert result.details["execution"]["started"] is False


def test_custom_mode_names_and_permissions_persist_without_changing_active_run(make_session):
    session = make_session()
    manager = session.services["sandbox"]
    manager.save_mode("offline", "Offline inspection", True, False, False)
    manager.select_mode("offline")
    assert manager.selected_mode.name == "Offline inspection"
    assert manager.mode_for(session.main).flags() == {"read_only": True, "workspace_only": False,
                                                     "network_allowed": False}
    restored = Settings.load(session.paths)
    assert restored.get("permissions.modes.offline.name") == "Offline inspection"
    assert restored.get("permissions.mode") == "workspace-edit"
    manager.save_mode("read-only", "My read-only", False, True, True)
    assert not manager.modes["read-only"].read_only


def test_inactive_conversation_keeps_its_policy_when_another_selection_changes(make_session):
    session = make_session(persist=True)
    manager = session.services["sandbox"]
    manager.select_mode("read-only")
    previous = session.main
    session.new_session()
    manager.select_mode("no-limit")
    previous.prepare_run()
    assert manager.mode_for(previous).read_only
    assert not manager.mode_for(session.main).read_only


def test_child_created_before_mode_change_keeps_its_origin_permissions(make_session):
    session = make_session()
    manager = session.services["sandbox"]
    manager.select_mode("read-only")
    child = session.create_runtime("queued-child", "subagent", parent=session.main)
    try:
        manager.select_mode("no-limit")
        faux.push("test", faux.reply(tools=[("write", {"path": "child.txt", "content": "bad"})]),
                  faux.reply("done"))
        child.agent.prompt("Inspect the workspace")
        assert manager.mode_for(child).read_only
        assert not (session.cwd / "child.txt").exists()
    finally:
        session.release_runtime(child)


def test_default_profile_is_workspace_edit_with_network(project):
    session = AgentSession(project, model="faux/test", session=None, extensions=False, refresh_models=False)
    try:
        mode = session.services["sandbox"].mode_for(session.main)
        assert mode.id == "workspace-edit"
        assert mode.flags() == {"read_only": False, "workspace_only": True, "network_allowed": True}
    finally:
        session.shutdown()


@pytest.mark.parametrize("legacy,identifier,flags", [
    ("read-only", "legacy-read-only", (True, True, False)),
    ("workspace-write", "legacy-workspace-write", (False, True, False)),
    ("disabled", "no-limit", (False, False, True)),
])
def test_explicit_legacy_policy_overrides_colliding_custom_preset(make_session, project, legacy, identifier, flags):
    """A preset name collision must not redefine an explicit compatibility option.

    Failure modes: legacy read-only writes data, workspace-write escapes its
    scope or enables networking, and disabled inherits a redefined No-Limit.
    """
    inside, outside = project / "legacy-inside.txt", project.parent / "legacy-outside.txt"
    collision = {"name": "Collision", "readOnly": legacy == "disabled",
                 "workspaceOnly": legacy == "disabled", "networkAllowed": legacy != "disabled"}
    session = make_session(
        faux.reply(tools=[("write", {"path": str(inside), "content": "inside"}),
                          ("write", {"path": str(outside), "content": "outside"})]), faux.reply("done"),
        settings={"profile": "minimal", "sandbox": {"mode": legacy},
                  "permissions": {"modes": {identifier: collision}}},
        extensions=False, refresh_models=False,
    )
    session.prompt("Apply the explicit legacy permission option")
    read_only, workspace_only, network_allowed = flags
    assert inside.exists() is not read_only
    assert outside.exists() is (not read_only and not workspace_only)
    assert [result.is_error for result in tool_results(session)] == [read_only, read_only or workspace_only]
    assert session.services["sandbox"].mode_for(session.main).flags() == {
        "read_only": read_only, "workspace_only": workspace_only, "network_allowed": network_allowed,
    }
