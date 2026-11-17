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


def test_permissions_owns_presets_and_approval(make_session):
    session = make_session()
    names = session.commands.names()
    assert "permissions" in names
    assert "deprecated" in session.commands.get("mode").description
    assert "deprecated" in session.commands.get("sandbox").description

    session.prompt("/permissions workspace-edit")
    assert session.services["sandbox"].selected_mode.id == "workspace-edit"
    session.prompt("/permissions read-only")
    assert session.services["sandbox"].selected_mode.id == "read-only"
    session.prompt("/permissions ask")
    assert session.approval_policy == "ask"


def test_deprecated_command_aliases_warn_and_keep_working(make_session):
    session = make_session()
    session.prompt("/mode read-only")
    assert session.services["sandbox"].selected_mode.id == "read-only"
    assert "deprecated" in session.ui.text
    session.ui.lines.clear()
    session.prompt("/sandbox")
    assert "deprecated" in session.ui.text


def test_removed_task_commands_are_not_registered(make_session):
    session = make_session()
    for name in ("plan", "review", "workflow"):
        assert session.commands.get(name) is None
    assert session.tool("propose_plan") is None
    assert session.tool("git_read") is None
    session.prompt("/plan")
    assert "removed" in session.ui.text.lower()
