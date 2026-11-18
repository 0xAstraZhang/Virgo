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
        with pytest.raises(ValueError, match="retired|requires|unknown"):
            AgentSession(project, model="faux/test", session=None, refresh_models=False, extensions=False,
                         settings={"permissions": {"mode": configured}})
        return
    session = AgentSession(project, model="faux/test", session=None, refresh_models=False, extensions=False)
    try:
        assert session.approval_policy == "ask"
        assert session.services["execution"].name == "host"
    finally:
        session.shutdown()


def test_legacy_permission_keys_read_through_the_canonical_spelling(project, tmp_path, monkeypatch):
    user = tmp_path / "legacy-user"
    user.mkdir()
    (user / "settings.json").write_text(json.dumps({
        "workMode": "plan", "execution": {"provider": "sandbox"},
        "permissions": {"mode": "workspace-edit", "modes": {"offline": {
            "name": "Offline", "readPaths": ["."], "writePaths": [], "networkAllowed": False}}}}))
    monkeypatch.setenv("VIRGO_HOME", str(user))
    settings = Settings.load(Paths(project))
    assert settings.get("execution.mode") == "workspace-edit"
    assert settings.get("permissions.modes") is None
    assert settings.get("workMode") is None
    assert any("workMode" in notice for notice in settings.migration_warnings)


def test_home_directory_is_not_reread_as_project_settings(tmp_path, monkeypatch):
    """Launching from the Virgo home must not reject the user's own grants."""
    home = tmp_path / "home"
    monkeypatch.delenv("VIRGO_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    state = home / ".virgo"
    state.mkdir()
    (state / "settings.json").write_text(json.dumps({
        "model": "faux/user", "execution": {"provider": "sandbox"},
        "permissions": {"mode": "workspace-edit", "modes": {"offline": {
            "name": "Offline", "readPaths": ["."], "writePaths": [], "networkAllowed": False}}},
    }))
    paths = Paths(home)
    assert paths.user == paths.project
    settings = Settings.load(paths)
    assert settings.get("model") == "faux/user"
    assert settings.get("execution.mode") == "workspace-edit"
    assert settings.get("permissions.presets") is None
    assert any("state directory" in notice for notice in settings.migration_warnings)


def test_project_layer_still_rejects_grants_when_the_paths_differ(project, tmp_path):
    home = tmp_path / "home"
    (home / "settings.json").write_text(json.dumps({"permissions": {"preset": "workspace-edit"}}))
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"permissions": {"preset": "workspace-edit"}}))
    with pytest.raises(ValueError, match="Project permissions"):
        Settings.load(Paths(project))


def test_invalid_legacy_modes_value_is_a_configuration_error(project):
    with pytest.raises(ValueError, match="permissions.modes"):
        Settings.load(Paths(project), {"permissions": {"modes": "nope", "presets": {}}})


def test_reserved_permission_preset_ids_are_rejected(project):
    with pytest.raises(ValueError, match="Cannot migrate"):
        Settings.load(Paths(project), {"permissions": {"preset": "ask", "presets": {"ask": {
            "name": "Ask", "readPaths": ["."], "writePaths": [], "networkAllowed": False}}}})


def test_invalid_presets_value_is_a_configuration_error(project):
    with pytest.raises(ValueError, match="permissions.presets"):
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


@pytest.mark.parametrize(("selection", "expected"), [
    (True, ("allow", "allow", "allow")),
    (False, ("deny", "deny", "deny")),
    (["egress"], ("allow", "deny", "deny")),
    (["ingress", "hostLoopback"], ("deny", "allow", "allow")),
])
def test_network_directions_survive_preset_resolution_and_native_mapping(tmp_path, selection, expected):
    from virgo.harness.execution_modes import migrate_preset, network_config, parse_network

    directions = parse_network(selection)
    assert network_config(directions) == selection
    assert tuple("allow" if d in directions else "deny" for d in ("egress", "ingress", "hostLoopback")) == expected
    entries = {"old": {"readPaths": ["."], "writePaths": ["."], "networkAllowed": selection}}
    if selection is True:
        assert migrate_preset("old", entries, tmp_path) == "workspace-edit"
    else:
        with pytest.raises(ValueError, match="Cannot migrate"):
            migrate_preset("old", entries, tmp_path)


@pytest.mark.parametrize("invalid", [["public"], ["egress", "egress"], ["ingress", True], 1, "egress"])
def test_invalid_network_directions_rejected(invalid):
    from virgo.harness.execution_modes import parse_network

    with pytest.raises(ValueError, match="networkAllowed"):
        parse_network(invalid)


def test_unselected_legacy_presets_cannot_widen_to_no_limit(project):
    config = {"permissions": {"presets": {"workspace-edit": {"networkAllowed": False}}}}
    with pytest.raises(ValueError, match="choose execution.mode"):
        Settings.load(Paths(project), config)
