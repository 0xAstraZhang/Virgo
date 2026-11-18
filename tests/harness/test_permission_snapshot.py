"""A run captures one permission decision; later selection changes apply to the next run."""
from __future__ import annotations

from virgo.harness.execution_modes import PermissionSnapshot


def test_snapshot_freezes_preset_approval_and_revision(broker_session):
    session = broker_session(approval="ask")
    runtime = session.main
    runtime.prepare_run()
    snapshot = runtime.permission_snapshot
    assert isinstance(snapshot, PermissionSnapshot)
    original = session.services["execution"].selected_mode
    assert snapshot.mode == original
    assert snapshot.approval == "ask"

    session.services["execution"].select_mode("read-only")
    assert runtime.permission_snapshot is snapshot
    assert runtime.permission_snapshot.mode == original
    assert session.services["execution"].selected_mode.id == "read-only"


def test_child_snapshot_keeps_the_parent_ceiling(broker_session):
    session = broker_session(approval="never")
    session.services["execution"].select_mode("read-only")
    child = session.create_runtime("child", "subagent", parent=session.main)
    try:
        child.prepare_run()
        assert not child.permission_snapshot.mode.write_paths
        assert child.permission_snapshot.approval == "never"
    finally:
        session.release_runtime(child)


def test_active_run_keeps_the_captured_approval_policy(broker_session):
    session = broker_session(approval="never")
    runtime = session.main
    runtime.prepare_run()
    session.approval_policy = "ask"
    assert runtime.permission_snapshot.approval == "never"


def test_next_run_captures_a_new_approval_policy(broker_session):
    session = broker_session(approval="never")
    runtime = session.main
    runtime.prepare_run()
    assert runtime.permission_snapshot.approval == "never"
    session.approval_policy = "ask"
    runtime.prepare_run()
    assert runtime.permission_snapshot.approval == "ask"


def test_isolated_task_refreshes_the_parent_snapshot_before_creating_a_child(broker_session):
    session = broker_session(approval="never")
    runtime = session.main
    runtime.prepare_run()
    session.approval_policy = "ask"
    runtime.refresh_permission_snapshot()
    assert runtime.permission_snapshot.approval == "ask"
    child = session.create_runtime("child", "subagent", parent=runtime)
    try:
        child.prepare_run()
        assert child.permission_snapshot.approval == "ask"
    finally:
        session.release_runtime(child)


def test_child_without_a_snapshot_inherits_the_parent_capture(broker_session):
    session = broker_session(approval="ask")
    runtime = session.main
    runtime.prepare_run()
    session.approval_policy = "never"
    child = session.create_runtime("child", "subagent", parent=runtime)
    try:
        child.prepare_run()
        assert child.permission_snapshot.approval == "ask"
    finally:
        session.release_runtime(child)

def test_opening_a_stored_workspace_binds_new_tools_and_children_to_its_cwd(broker_session, tmp_path):
    from virgo.ai.types import UserMessage
    from virgo.harness.store import SessionStore

    session = broker_session(persist=True)
    original = session.main
    workspace = tmp_path / "opened-project"
    workspace.mkdir()
    store = SessionStore.new(session.paths.sessions, workspace)
    store.append_message(UserMessage("Resume this project"))
    session.open_session(store.path)
    assert session.main.cwd == workspace == session.main.execution_context.workspace
    child = session.create_runtime("opened-child", "subagent", parent=session.main)
    try:
        assert child.cwd == workspace == child.execution_context.workspace
        assert original.execution_context.workspace == original.cwd != workspace
    finally:
        session.release_runtime(child)
