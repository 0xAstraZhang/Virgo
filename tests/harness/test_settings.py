"""Settings locations, environment overrides, and explicit approval migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from virgo.harness.session import AgentSession
from virgo.harness.settings import Paths, Settings


def test_default_user_and_project_directories(project, tmp_path, monkeypatch):
    monkeypatch.delenv("VIRGO_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    paths = Paths(project)
    assert paths.user == tmp_path / ".virgo"
    assert paths.project == project / ".virgo"


def test_home_and_model_overrides(project, tmp_path, monkeypatch):
    user = tmp_path / "custom-user"
    user.mkdir()
    (user / "settings.json").write_text(json.dumps({"model": "faux/user", "effort": "low"}))
    state = project / ".virgo"
    state.mkdir()
    (state / "settings.json").write_text(json.dumps({"model": "faux/project"}))
    monkeypatch.setenv("VIRGO_HOME", str(user))
    paths = Paths(project)
    assert paths.user == user
    assert Settings.load(paths).get("model") == "faux/project"
    monkeypatch.setenv("VIRGO_MODEL", "faux/environment")
    settings = Settings.load(paths)
    assert settings.get("model") == "faux/environment"
    assert settings.get("effort") == "low"
    assert Settings.load(paths, {"model": "faux/explicit"}).get("model") == "faux/explicit"


@pytest.mark.parametrize(
    "configured",
    [pytest.param(None, id="None-auto-review"), pytest.param("default", id="default-auto-review"),
     pytest.param("accept-edits", id="accept-edits-auto-review"), pytest.param("yolo", id="yolo-accept-all")],
)
def test_permission_defaults_and_saved_mode_migration(project, configured):
    """Retain historical case identities while retiring silent mode aliases."""
    if configured is not None:
        with pytest.raises(ValueError, match="(?i)retired|execution\\.mode"):
            AgentSession(project, model="faux/test", session=None, refresh_models=False, extensions=False,
                         settings={"permissions": {"mode": configured}})
        return
    session = AgentSession(project, model="faux/test", session=None, refresh_models=False, extensions=False)
    try:
        from virgo.execution_policy import default_execution_mode

        expected_mode = default_execution_mode()
        assert session.approval_policy == "ask"
        assert session.services["execution"].name == ("host" if expected_mode == "no-limit" else "sandbox")
        assert session.services["execution"].selected_mode_id == expected_mode
    finally:
        session.shutdown()


def test_legacy_permission_keys_read_through_the_canonical_spelling(project, tmp_path, monkeypatch):
    """S7-02: Retired permissions.mode/modes keys fail clearly instead of silently migrating."""
    user = tmp_path / "legacy-user"
    user.mkdir()
    (user / "settings.json").write_text(json.dumps({
        "workMode": "plan",
        "permissions": {"mode": "workspace-edit", "modes": {"offline": {
            "name": "Offline", "readOnly": True, "workspaceOnly": True, "networkAllowed": False}}}}))
    monkeypatch.setenv("VIRGO_HOME", str(user))
    with pytest.raises(ValueError, match="Retired permissions setting.*execution\\.mode"):
        Settings.load(Paths(project))


def test_home_directory_is_not_reread_as_project_settings(tmp_path, monkeypatch):
    """Launching from the Virgo home must not reject the user's own settings."""
    home = tmp_path / "home"
    monkeypatch.delenv("VIRGO_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    state = home / ".virgo"
    state.mkdir()
    (state / "settings.json").write_text(json.dumps({
        "model": "faux/user",
        "execution": {"mode": "workspace-edit"},
    }))
    paths = Paths(home)
    assert paths.user == paths.project
    settings = Settings.load(paths)
    assert settings.get("model") == "faux/user"
    assert settings.get("execution.mode") == "workspace-edit"
    assert not settings.migration_warnings


def test_project_layer_still_rejects_grants_when_the_paths_differ(project, tmp_path):
    home = tmp_path / "home"
    (home / "settings.json").write_text(json.dumps({"execution": {"mode": "workspace-edit"}}))
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": {"approval": "never"}}))
    with pytest.raises(ValueError, match="Project permissions"):
        Settings.load(Paths(project))


def test_invalid_legacy_modes_value_is_a_configuration_error(project):
    with pytest.raises(ValueError, match="Retired permissions setting.*execution\\.mode"):
        Settings.load(Paths(project), {"permissions": {"modes": "nope", "presets": {}}})


def test_reserved_permission_preset_ids_are_rejected(project):
    with pytest.raises(ValueError, match="Retired permissions setting.*execution\\.mode"):
        Settings.load(Paths(project), {"permissions": {"preset": "ask", "presets": {"ask": {
            "name": "Ask", "readOnly": True, "workspaceOnly": True, "networkAllowed": False}}}})


def test_invalid_presets_value_is_a_configuration_error(project):
    with pytest.raises(ValueError, match="Retired permissions setting.*execution\\.mode"):
        Settings.load(Paths(project), {"permissions": {
            "modes": {"offline": {"name": "Offline", "readOnly": True,
                                  "workspaceOnly": True, "networkAllowed": False}},
            "presets": "nope",
        }})


def test_retired_task_configuration_is_ignored_with_a_notice(project, tmp_path, monkeypatch):
    user = tmp_path / "retired-user"
    user.mkdir()
    (user / "settings.json").write_text(json.dumps({
        "plan": True,
        "profile": "minimal",
        "workMode": "plan",
        "plugins": {"plan": {"enabled": True}, "code_review": {"enabled": True}},
    }))
    monkeypatch.setenv("VIRGO_HOME", str(user))
    settings = Settings.load(Paths(project))
    assert settings.get("plan") is None
    assert settings.get("profile") is None
    assert set(settings.get("plugins", {})) == set()
    notices = "\n".join(settings.migration_warnings)
    for name in ("plan", "profile", "workMode", "plugins.plan", "plugins.code_review"):
        assert name in notices


@pytest.mark.parametrize(("selection", "expected"), [(True, True), (False, False)])
def test_network_boolean_setting_parsed(selection, expected):
    from virgo.execution_policy import parse_network

    assert parse_network(selection) is expected


@pytest.mark.parametrize("invalid", [["public"], ["egress"], ["ingress", True], 1, "egress", None])
def test_invalid_network_values_rejected(invalid):
    from virgo.execution_policy import parse_network

    with pytest.raises(ValueError, match="boolean"):
        parse_network(invalid)


def test_unselected_legacy_presets_cannot_widen_to_no_limit(project):
    config = {"permissions": {"presets": {"workspace-edit": {"networkAllowed": False}}}}
    with pytest.raises(ValueError, match="execution\\.mode"):
        Settings.load(Paths(project), config)


@pytest.mark.parametrize(("saved", "override"), [("no-limit", "workspace-edit"), ("read-only", "read-only")])
def test_explicit_sandbox_override_keeps_its_boundary_with_a_saved_mode(project, saved, override):
    paths = Paths(project)
    (paths.user / "settings.json").write_text(json.dumps({"execution": {"mode": saved}}))
    with pytest.raises(ValueError, match="Unsupported execution setting.*provider"):
        Settings.load(paths, {"execution": {"provider": "sandbox"}})
    settings = Settings.load(paths, {"execution": {"mode": override}})
    assert settings.get("execution.mode") == override


@pytest.mark.parametrize(("config", "explicit"), [
    ({"permissions": {"presets": {"offline": {"name": "Offline", "readOnly": True,
                                              "workspaceOnly": True, "networkAllowed": False}}}},
     None),
    ({"sandbox": {"mode": "disabled"}, "permissions": {"preset": "read-only"}}, None),
    ({"sandbox": {"mode": "workspace-write"}}, "read-only"),
])
def test_native_settings_migrate_from_the_effective_main_policy(project, config, explicit):
    """S7-02: Legacy sandbox/permissions.preset configs fail loudly instead of silently migrating."""
    paths = Paths(project)
    (paths.user / "settings.json").write_text(json.dumps(config))
    overrides = {"execution": {"mode": explicit}} if explicit else None
    with pytest.raises(ValueError, match="execution\\.mode"):
        Settings.load(paths, overrides)


@pytest.mark.parametrize("previous", [{}, {"language": "zh-CN", "model": "faux/old"}])
def test_upgraded_user_keeps_previous_implicit_workspace_boundary(project, previous):
    from virgo.execution_policy import default_execution_mode

    expected = default_execution_mode()
    paths = Paths(project)
    path = paths.user / "settings.json"
    original = json.dumps(previous)
    path.write_text(original)
    assert Settings.load(paths).get("execution.mode") == expected
    assert Settings.load(paths, {"execution": {"mode": "no-limit"}}).get("execution.mode") == "no-limit"
    assert path.read_text() == original


def test_first_user_save_preserves_fresh_default_across_projects_and_cli_overrides(project, tmp_path):
    from virgo.execution_policy import default_execution_mode

    expected = default_execution_mode()
    paths = Paths(project)
    paths.project.mkdir()
    (paths.project / "settings.json").write_text(json.dumps({"effort": "low"}))
    assert Settings.load(paths).get("execution.mode") == expected
    path = paths.user / "settings.json"
    assert not path.exists()
    settings = Settings.load(paths, {"execution": {"mode": "read-only"}})
    settings.save_user({"language": "zh-CN"})
    assert settings.get("execution.mode") == "read-only"
    assert json.loads(path.read_text())["execution"]["mode"] == expected
    assert Settings.load(Paths(tmp_path / "other-project")).get("execution.mode") == expected


def test_extensions_disabled_validation_and_normalization(project):
    settings = Settings.load(Paths(project), {"extensions": {"disabled": ["todo", "mcp.py"]}})
    assert settings.disabled_extensions() == frozenset({"todo", "mcp"})

    for invalid in (
        {"extensions": "bad"},
        {"extensions": {"unknownKey": True}},
        {"extensions": {"disabled": "todo"}},
        {"extensions": {"disabled": [123]}},
        {"extensions": {"disabled": ["  "]}},
        {"extensions": {"disabled": [".py"]}},
        {"extensions": {"disabled": ["../todo"]}},
        {"extensions": {"disabled": ["sub\\todo"]}},
    ):
        with pytest.raises(ValueError):
            Settings.load(Paths(project), invalid)
