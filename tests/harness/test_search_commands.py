"""/search shows and changes the search backend, in plain and fullscreen UIs."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from textual.widgets import OptionList

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.harness.search_commands import CHOICES
from virgo.harness.settings import Settings
from virgo.harness.ui import UI

from tests.support.tui import wait_for


class SearchUI(UI):
    interactive = True

    def __init__(self, choice):
        self.choice = choice
        self.lines = []
        self.offered = []

    def select(self, question, options):
        self.offered = options
        return next((label for label, value in CHOICES.items() if value == self.choice), None)

    def print(self, text, style=None):
        self.lines.append(text)


@pytest.fixture(autouse=True)
def no_deepseek_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_search_status_is_read_only_in_headless_mode(make_session):
    session = make_session()
    session.prompt("/search")
    assert "search: auto" in session.ui.text
    assert "/search [auto|duckduckgo|deepseek|off]" in session.ui.text
    assert not (session.paths.user / "settings.json").exists()
    assert not session.main.messages


@pytest.mark.parametrize("argument,mode,backend", [
    ("auto", "auto", "duckduckgo"),
    ("duckduckgo", "builtin", "duckduckgo"),
    ("ddg", "builtin", "duckduckgo"),
    ("deepseek", "builtin", "deepseek"),
    ("DS", "builtin", "deepseek"),
    ("off", "off", "duckduckgo"),
])
def test_explicit_selection_persists_and_keeps_model(make_session, monkeypatch, argument, mode, backend):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "search-test-secret")
    session = make_session()
    session.settings.save_user({"effort": "high", "search": {"timeout": 21}})
    original_model = session.model
    session.prompt(f"/search {argument}")
    assert session.settings.get("search.mode") == mode
    assert session.settings.get("search.backend") == backend
    assert session.services["search"].selection == (mode if mode in ("auto", "off") else backend)
    reloaded = Settings.load(session.paths)
    assert reloaded.get("search.mode") == mode
    assert reloaded.get("search.backend") == backend
    assert reloaded.get("search.timeout") == 21 and reloaded.get("effort") == "high"
    assert session.model is original_model
    assert not session.main.messages
    assert "saved" in session.ui.text
    assert "search-test-secret" not in (session.paths.user / "settings.json").read_text()
    assert "search-test-secret" not in session.ui.text


def test_interactive_selection_and_cancellation(make_session):
    ui = SearchUI("duckduckgo")
    session = make_session(ui=ui)
    session.prompt("/search")
    assert ui.offered == list(CHOICES)
    assert session.services["search"].selection == "duckduckgo"
    path = session.paths.user / "settings.json"
    saved = path.read_bytes()
    ui.choice = None
    session.prompt("/search")
    assert path.read_bytes() == saved
    assert session.services["search"].selection == "duckduckgo"
    assert "Kept search as duckduckgo" in ui.lines


@pytest.mark.parametrize("argument", ["brave", "unknown", "auto extra", "--help"])
def test_invalid_selection_leaves_configuration_unchanged(make_session, argument):
    session = make_session()
    before = copy.deepcopy(session.settings.data)
    session.prompt(f"/search {argument}")
    assert session.settings.data == before
    assert not (session.paths.user / "settings.json").exists()
    assert "Usage: /search" in session.ui.text


def test_deepseek_selection_without_key_explains_setup_and_keeps_selection(make_session):
    session = make_session()
    session.prompt("/search ddg")
    path = session.paths.user / "settings.json"
    saved = path.read_bytes()
    session.prompt("/search ds")
    assert session.services["search"].selection == "duckduckgo"
    assert path.read_bytes() == saved
    assert "/login api deepseek" in session.ui.text
    assert "DEEPSEEK_API_KEY" in session.ui.text


def test_deepseek_selection_reuses_saved_login_without_switching_model(make_session):
    session = make_session()
    session.auth.set("deepseek", {
        "type": "api_key", "key": "saved-search-key",
        "connection": {"api": "openai", "baseUrl": "https://api.deepseek.com"},
    })
    session.prompt("/search deepseek")
    assert session.services["search"].selection == "deepseek"
    assert session.model.spec == "faux/test"
    settings = (session.paths.user / "settings.json").read_text()
    assert json.loads(settings)["search"]["backend"] == "deepseek"
    assert "saved-search-key" not in settings + session.ui.text


def test_failed_save_keeps_current_selection(make_session, monkeypatch):
    session = make_session()

    def fail(_patch):
        raise OSError("settings are read-only")

    monkeypatch.setattr(session.settings, "save_user", fail)
    session.prompt("/search duckduckgo")
    assert session.services["search"].selection == "auto"
    assert "settings are read-only" in session.ui.text


def test_project_override_is_explained_when_saving_user_default(make_session, project):
    config = project / ".virgo" / "settings.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"search": {"mode": "builtin", "backend": "duckduckgo"}}))
    session = make_session()
    session.prompt("/search auto")
    assert session.services["search"].selection == "auto"
    assert "saved as user default" in session.ui.text
    assert "project/runtime settings take precedence on restart" in session.ui.text
    assert Settings.load(session.paths).get("search.mode") == "builtin"


def test_initially_disabled_search_can_be_enabled_immediately(make_session):
    session = make_session(settings={"search": {"mode": "off"}})
    assert not any(t.name == "web_search" for t in session.main.request_context([]).tools)
    session.prompt("/search ddg")
    assert any(t.name == "web_search" for t in session.main.request_context([]).tools)
    session.prompt("/search off")
    assert not any(t.name == "web_search" for t in session.main.request_context([]).tools)


def test_fullscreen_search_picker_applies_selection_without_chat_message(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(90, 26)) as pilot:
            app.controller.submit("/search")
            await wait_for(pilot, lambda: app.input_request is not None)
            choices = app.query_one("#completions", OptionList)
            assert choices.option_count == 4 and choices.highlighted == 0
            assert len(app.screen_stack) == 1
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: not app.controller.busy and app.input_request is None)
            assert session.services["search"].selection == "duckduckgo"
            assert session.model.spec == "faux/test"
            assert not session.main.messages
            assert json.loads((session.paths.user / "settings.json").read_text())["search"]["backend"] == "duckduckgo"

    asyncio.run(scenario())
