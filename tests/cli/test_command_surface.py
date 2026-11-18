"""The permission and task axes expose one canonical command each."""
from __future__ import annotations

from virgo.cli.main import build_parser


def test_canonical_cli_flags_and_deprecated_aliases():
    parser = build_parser("en")
    args = parser.parse_args(["--permission", "read-only", "--approval", "never", "-p", "x"])
    assert args.permission == "read-only"
    assert args.approval == "never"

    legacy = parser.parse_args(["--mode", "read-only", "-p", "x"])
    assert legacy.legacy_mode == "read-only"


def test_permissions_owns_presets_and_approval(broker_session):
    session = broker_session()
    names = session.commands.names()
    assert "permissions" in names
    assert session.commands.get("mode") is None
    assert session.commands.get("sandbox") is None

    session.prompt("/permissions workspace-edit")
    assert session.services["execution"].selected_mode.id == "workspace-edit"
    session.prompt("/permissions read-only")
    assert session.services["execution"].selected_mode.id == "read-only"
    session.prompt("/permissions ask")
    assert session.approval_policy == "ask"


def test_host_status_has_no_resource_approval_controls(make_session):
    session = make_session()
    session.prompt("/permissions show")
    assert "No-Limit (Host)" in session.ui.text
    session.prompt("/permissions list")
    assert all(label in session.ui.text for label in ("Read-Only", "Workspace-Edit", "No-Limit"))
    assert session.services["execution"].sandbox is None


def test_removed_task_commands_are_not_registered(make_session):
    session = make_session()
    for name in ("plan", "review", "workflow", "todos"):
        assert session.commands.get(name) is None
    assert session.tool("propose_plan") is None
    assert session.tool("git_read") is None
    session.prompt("/plan")
    assert "removed" in session.ui.text.lower()
