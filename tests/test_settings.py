from __future__ import annotations

import json
from pathlib import Path

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
