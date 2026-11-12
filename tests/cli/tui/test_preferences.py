"""The inline preference panels preview choices and only commit on confirmation."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from textual.widgets import OptionList, Static

from virgo.ai.catalog import CatalogCancelled, CatalogModel
from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.preferences import PreferencePicker
from virgo.harness.preferences import PreferenceChoice

from tests.support.terminal import PtyClient, plain
from tests.support.tui import wait_for


def test_escape_cancels_live_catalog_loading_without_opening_stale_picker(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    entered = threading.Event()

    def stalled(_model, signal):
        entered.set()
        assert signal.wait(2)
        raise CatalogCancelled("Model discovery cancelled.")

    monkeypatch.setattr("virgo.harness.preferences.fetch_model_catalog", stalled)

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/model")
            await wait_for(pilot, entered.is_set)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert not app.query_one(PreferencePicker).display
            assert app.editor.display and app.active_input is None

    asyncio.run(scenario())


def test_effort_panel_previews_cancels_then_saves_default(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/effort")
            panel = app.query_one(PreferencePicker)
            await wait_for(pilot, lambda: panel.display and app.active_input is not None)
            assert not app.editor.display
            assert len(app.screen_stack) == 1
            assert panel.request.kind == "effort"
            assert "Faster" in str(panel.query_one("#preference-effort", Static).content)
            await pilot.press("right", "right")
            assert panel.effort == "xhigh"
            assert session.effort == "medium"
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert session.effort == "medium"
            assert not (session.paths.user / "settings.json").exists()
            app.controller.submit("/effort")
            await wait_for(pilot, lambda: panel.display)
            await pilot.press("left", "enter")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert session.effort == "low"
            assert json.loads((session.paths.user / "settings.json").read_text())["effort"] == "low"
            assert not panel.display and app.focused is app.editor
            assert not session.main.messages and not faux.CALLS["test"]

    asyncio.run(scenario())


def test_model_panel_switches_custom_model_and_effort_for_session_only(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(
            ui=ui,
            settings={
                "providers": {"local": {"api": "faux", "models": {"custom": {"contextWindow": 64000}}}}
            },
        )
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/model")
            panel = app.query_one(PreferencePicker)
            await wait_for(pilot, lambda: panel.display)
            models = panel.query_one("#preference-models", OptionList)
            assert panel.providers[models.highlighted] == "Faux"
            custom = panel.providers.index("local")
            for _ in range(custom):
                await pilot.press("down")
            await pilot.press("enter")
            assert panel.provider == "local" and panel.visible_models[0].spec == "local/custom"
            await pilot.press("right")
            assert session.model.spec == "faux/test" and session.effort == "medium"
            await pilot.press("s")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert session.model.spec == session.main.model.spec == "local/custom"
            assert session.effort == "high"
            assert not (session.paths.user / "settings.json").exists()
            assert not faux.CALLS["test"]
            assert app.editor.display and not panel.display
            await wait_for(pilot, lambda: "custom · high" in str(app.query_one("#status", Static).content))

    asyncio.run(scenario())


def test_provider_group_combines_api_and_account_models_and_back_keeps_selection(make_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "virgo.harness.preferences.fetch_model_catalog",
        lambda model, signal=None: [CatalogModel("configured-chat" if model.provider == "openai" else "gpt-6.1-terra")],
    )

    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, settings={"providers": {"openai": {"models": {"configured-chat": {}}}}})
        session.auth.set("openai-codex", {
            "type": "oauth", "access": "test-access", "expires": time.time() + 3600,
            "accountId": "test-account",
        })
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/model")
            panel = app.query_one(PreferencePicker)
            await wait_for(pilot, lambda: panel.display)
            assert panel.providers.count("OpenAI") == 1
            options = panel.query_one("#preference-models", OptionList)
            options.highlighted = panel.providers.index("OpenAI")
            await pilot.press("enter")
            assert panel.provider == "OpenAI"
            assert {model.spec for model in panel.visible_models} == {
                "openai/configured-chat", "openai-codex/gpt-6.1-terra",
            }
            await pilot.press("backspace")
            assert panel.provider is None and panel.providers[options.highlighted] == "OpenAI"
            assert session.model.spec == "faux/test" and not (session.paths.user / "settings.json").exists()
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)

    asyncio.run(scenario())


def test_model_panel_saves_default_and_cancel_keeps_it(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, settings={"providers": {"faux": {"models": {"test": {}, "other": {}}}}})
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/model")
            panel = app.query_one(PreferencePicker)
            await wait_for(pilot, lambda: panel.display)
            assert panel.provider is None
            await pilot.press("enter")
            await pilot.pause()
            models = panel.query_one("#preference-models", OptionList)
            await pilot.click(models, offset=(4, 1))
            assert models.highlighted == 1
            assert panel.display and session.model.spec == "faux/test"
            assert not (session.paths.user / "settings.json").exists()
            await pilot.press("right")
            selected = panel.visible_models[panel.query_one(OptionList).highlighted].spec
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy)
            saved = json.loads((session.paths.user / "settings.json").read_text())
            assert saved["model"] == selected and saved["effort"] == "high"
            app.controller.submit("/model")
            await wait_for(pilot, lambda: panel.display)
            await pilot.press("enter", "down", "left", "backspace")
            assert panel.provider is None
            assert "Select provider" in str(panel.query_one("#preference-title", Static).content)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert session.model.spec == selected and session.effort == "high"
            assert json.loads((session.paths.user / "settings.json").read_text()) == saved

    asyncio.run(scenario())


def test_picker_waits_for_active_run_and_stays_usable_when_resized(make_session):
    entered, release = threading.Event(), threading.Event()

    def response(_):
        entered.set()
        assert release.wait(4)
        return faux.reply("done")

    async def scenario():
        ui = FullscreenUI()
        session = make_session(response, ui=ui)
        app = VirgoApp(session, ui, initial="work")
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await wait_for(pilot, entered.is_set)
                app.controller.submit("/model")
                panel = app.query_one(PreferencePicker)
                assert not panel.display
                assert app.controller.pending == ["/model"]
                release.set()
                await wait_for(pilot, lambda: panel.display)
                for width, height in ((40, 16), (80, 24), (120, 40)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    assert panel.region.bottom <= app.query_one("#status").region.y
                    assert panel.region.height > 0 and panel.region.right <= width
                    assert app.query_one("#status").region.bottom <= height
                    await pilot.press("down", "right")
                await pilot.press("escape")
                await wait_for(pilot, lambda: not app.controller.busy)
                assert session.model.spec == "faux/test" and session.effort == "medium"
                assert len(faux.CALLS["test"]) == 1
        finally:
            release.set()

    asyncio.run(scenario())


def test_closing_with_picker_open_releases_worker(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/effort")
            await wait_for(pilot, lambda: app.query_one(PreferencePicker).display)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.controller.closed)
            assert not ui._inputs

    asyncio.run(scenario())


def test_picker_keeps_status_visible_with_scrolled_history_and_queued_commands(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, settings={"providers": {"faux": {"models": {"test": {}, "other": {}}}}})
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.transcript.add_notice("\n".join(f"old line {i}" for i in range(80)))
            await pilot.pause()
            app.transcript.action_scroll_home()
            await wait_for(pilot, lambda: app.query_one("#jump").display)
            app.controller.submit("/model")
            panel = app.query_one(PreferencePicker)
            await wait_for(pilot, lambda: panel.display)
            await pilot.press("enter", "down")
            models = panel.query_one("#preference-models", OptionList)
            selected = models.highlighted
            selected_name = panel.visible_models[selected].label
            for _ in range(3):
                app.controller.submit("/effort")
            for width, height in ((80, 24), (40, 16), (80, 24)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert not app.query_one("#jump").display
                assert len(str(app.query_one("#pending", Static).content).splitlines()) == 3
                assert panel.region.bottom <= app.query_one("#status").region.y
                assert app.query_one("#status").region.bottom <= height
                assert panel.query_one("#preference-help").region.bottom <= panel.region.bottom
                assert models.highlighted == selected
                if width < 64:
                    assert models.size.height == 1
                    assert selected_name in models.render_line(0).text
                    assert "context" not in models.render_line(0).text
            app.controller.request_close()
            await wait_for(pilot, lambda: app.controller.closed)

    asyncio.run(scenario())


def test_repeated_confirmation_and_stale_answer_cannot_commit_next_picker(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            panel = app.query_one(PreferencePicker)
            app.controller.submit("/effort")
            await wait_for(pilot, lambda: panel.display)
            request = panel.request
            panel.action_effort(1)
            panel.action_choose(False)
            panel.action_choose(True)
            await wait_for(pilot, lambda: not app.controller.busy)
            assert session.effort == "high"
            assert not (session.paths.user / "settings.json").exists()
            app.controller.submit("/effort")
            await wait_for(pilot, lambda: panel.display)
            assert panel.request is not request
            panel.post_message(
                PreferencePicker.Answered(request, PreferenceChoice(request.model, "max", True))
            )
            await pilot.pause()
            assert panel.display and session.effort == "high"
            assert not (session.paths.user / "settings.json").exists()
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)

    asyncio.run(scenario())


@pytest.mark.pty
def test_pty_fast_arrow_and_confirm_selects_new_model_and_effort(project, tmp_path):
    home = tmp_path / "picker-home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"providers": {"faux": {"models": {"demo": {}, "other": {}}}}}))
    with PtyClient(project, home) as terminal:
        terminal.wait_for(lambda data: "demo" in plain(data))
        terminal.send(b"/model\r")
        terminal.wait_for(lambda data: "Select provider" in plain(data))
        # Enter the provider, navigate and confirm in one terminal read.
        terminal.send(b"\r\x1b[B\x1b[C\r")
        terminal.wait_for(lambda _: json.loads((home / "settings.json").read_text()).get("model") == "faux/other")
        saved = json.loads((home / "settings.json").read_text())
        assert saved["model"] == "faux/other"
        assert saved["effort"] == "high"
        terminal.send(b"\x04")
        assert terminal.finish() == 0
        terminal.assert_restored()
