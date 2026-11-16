"""The permission and task axes expose one canonical command each."""
from __future__ import annotations

from virgo.cli.main import build_parser


def test_canonical_cli_flags_and_deprecated_aliases():
    parser = build_parser("en")
    args = parser.parse_args(["--permission", "read-only", "--approval", "never", "-p", "x"])
    assert args.permission == "read-only"
    assert args.approval == "never"
    assert args.plan is False

    plan = parser.parse_args(["--plan", "-p", "x"])
    assert plan.plan is True

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


def test_plan_on_and_off_use_the_plan_command(make_session):
    session = make_session(settings={"profile": "minimal", "plugins": {"plan": {"enabled": True}}})
    permissions = session.services["permissions"]
    session.prompt("/plan")
    assert permissions.work_mode == "plan"
    session.prompt("/plan off")
    assert permissions.work_mode == "default"
