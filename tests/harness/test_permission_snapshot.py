"""A run captures one permission decision; later selection changes apply to the next run."""
from __future__ import annotations

from virgo.harness.execution_modes import PermissionSnapshot


def test_snapshot_freezes_preset_approval_and_revision(make_session):
    session = make_session(approval="ask")
    runtime = session.main
    runtime.prepare_run()
    snapshot = runtime.permission_snapshot
    assert isinstance(snapshot, PermissionSnapshot)
    original = session.services["sandbox"].selected_mode
    assert snapshot.mode == original
    assert snapshot.approval == "ask"

    session.services["sandbox"].select_mode("read-only")
    assert runtime.permission_snapshot is snapshot
    assert runtime.permission_snapshot.mode == original
    assert session.services["sandbox"].selected_mode.id == "read-only"


def test_child_snapshot_keeps_the_parent_ceiling(make_session):
    session = make_session(approval="never")
    child = session.create_runtime("child", "subagent", parent=session.main, read_only=True)
    try:
        child.prepare_run()
        assert child.permission_snapshot.mode.read_only
        assert child.permission_snapshot.approval == "never"
    finally:
        session.release_runtime(child)


def test_active_run_keeps_the_captured_approval_policy(make_session):
    session = make_session(approval="never")
    runtime = session.main
    runtime.prepare_run()
    session.approval_policy = "ask"
    permissions = session.services["permissions"]
    assert permissions.approval_for(runtime) == "never"
    assert runtime.permission_snapshot.approval == "never"


def test_next_run_captures_a_new_approval_policy(make_session):
    session = make_session(approval="never")
    runtime = session.main
    runtime.prepare_run()
    assert runtime.permission_snapshot.approval == "never"
    session.approval_policy = "ask"
    runtime.prepare_run()
    assert runtime.permission_snapshot.approval == "ask"
