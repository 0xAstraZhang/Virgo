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
        with pytest.raises(ValueError, match=r"permissions\.mode.*retired"):
            AgentSession(project, model="faux/test", session=None, refresh_models=False, extensions=False,
                         settings={"permissions": {"mode": configured}})
        return
    session = AgentSession(project, model="faux/test", session=None, refresh_models=False, extensions=False)
    try:
        assert session.approval_policy == "ask"
        assert session.services["sandbox"].mode == "workspace-write"
        session.services["permissions"].set_work_mode("plan")
        assert session.services["permissions"].work_mode == "plan"
        assert session.approval_policy == "ask"
    finally:
        session.shutdown()
