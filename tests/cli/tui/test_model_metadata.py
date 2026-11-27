"""Background model discovery reaches the picker and cached footer immediately."""

import asyncio

import pytest
from textual.widgets import Static

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.preferences import PreferencePicker

from tests.support.model_catalog import catalog_session, model_catalog
from tests.support.tui import wait_for


@pytest.mark.parametrize("available", [True, False], ids=["live", "unavailable"])
def test_startup_refresh_reaches_picker(tmp_path, monkeypatch, available):
    with model_catalog(monkeypatch) as catalog:
        if not available:
            catalog["status"] = 503

        async def scenario():
            ui = FullscreenUI()
            session = catalog_session(tmp_path, catalog, ui)
            app = VirgoApp(session, ui)
            try:
                async with app.run_test(size=(80, 24)) as pilot:
                    assert not session.models.refresh_done.is_set(), "Startup must not wait for a slow directory."
                    assert session.model.context_window == 500000
                    app.controller.submit("/model")
                    await wait_for(pilot, lambda: len(catalog["requests"]) == 2)
                    panel = app.query_one(PreferencePicker)
                    assert app.controller.busy and not panel.display
                    catalog["gate"].set()
                    await wait_for(pilot, session.models.refresh_done.is_set)
                    if available:
                        await wait_for(pilot, lambda: panel.display)
                        assert "local/grok-4.7" in {model.spec for model in panel.request.models}
                        await pilot.press("escape")
                    await wait_for(pilot, lambda: not app.controller.busy)
                    assert not panel.display and app.editor.display
                    assert session.model.context_window == (600000 if available else 500000)
                    for _ in range(2):
                        app.controller.submit("/model")
                        await wait_for(pilot, lambda: not app.controller.busy or panel.display)
                        await pilot.press("escape")
                        await wait_for(pilot, lambda: not app.controller.busy)
                    assert len(catalog["requests"]) == 2, "/model must never fetch."
            finally:
                catalog["gate"].set()
                session.shutdown()

            if not available:
                return
            catalog["status"] = 503
            catalog["gate"].clear()
            restarted_ui = FullscreenUI()
            restarted = catalog_session(tmp_path, catalog, restarted_ui)
            reopened = VirgoApp(restarted, restarted_ui)
            try:
                async with reopened.run_test(size=(80, 24)) as pilot:
                    assert not restarted.models.refresh_done.is_set()
                    assert "/600.0k" in str(reopened.query_one("#metrics", Static).content)
                    reopened.controller.submit("/model")
                    panel = reopened.query_one(PreferencePicker)
                    await wait_for(pilot, lambda: panel.display)
                    assert "local/grok-4.7" in {model.spec for model in panel.request.models}
                    assert not catalog["gate"].is_set(), "Cached choices must not wait for the network."
                    await pilot.press("escape")
                    await wait_for(pilot, lambda: not reopened.controller.busy)
                    catalog["gate"].set()
                    await wait_for(pilot, restarted.models.refresh_done.is_set)
                    assert restarted.model.context_window == 600000
                    assert len(catalog["requests"]) == 4
            finally:
                catalog["gate"].set()
                restarted.shutdown()

        asyncio.run(scenario())
