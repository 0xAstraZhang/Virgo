"""Authentication forms are local UI interactions, never model input."""

from __future__ import annotations

import asyncio
import threading

from textual.widgets import Input

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.dialogs import InputScreen
from virgo.harness.ui import UI

from .test_tui_app import wait_for
from .test_tui_controller import until


def test_headless_forms_do_not_wait():
    ui = UI()
    assert ui.input("API key", password=True) is None
    assert ui.select("Provider", ["one", "two"]) is None


def test_form_cancellation_and_shutdown_release_workers():
    for cancel in (lambda ui: ui.cancel_inputs(), lambda ui: ui.close()):
        ui = FullscreenUI()
        answers = []
        worker = threading.Thread(target=lambda ui=ui, answers=answers: answers.append(ui.input("API key", password=True)))
        worker.start()
        until(lambda ui=ui: bool(ui._inputs))
        cancel(ui)
        worker.join(1)
        assert answers == [None]
        assert not ui._inputs
        assert any(update.kind == "input_cancelled" for update in ui.drain())
    assert ui.input("Again") is None
    assert ui.select("Provider", ["one"]) is None


def test_fullscreen_select_password_and_default_stay_out_of_history(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        answers = []

        def login(s, _args):
            answers.append(s.ui.select("Login method", ["Auth account", "API key"]))
            answers.append(s.ui.input("API key", password=True))
            answers.append(s.ui.input("Model", default="deepseek-chat"))

        session.commands.register("testlogin", login)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.submit("/testlogin")
            await wait_for(pilot, lambda: isinstance(app.screen, InputScreen) and app.screen.is_mounted)
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: isinstance(app.screen, InputScreen) and app.screen.request.password)
            field = app.screen.query_one(Input)
            assert field.password
            field.value = "private-api-key"
            await pilot.press("enter")
            await wait_for(pilot, lambda: isinstance(app.screen, InputScreen) and app.screen.request.default)
            assert app.screen.query_one(Input).value == "deepseek-chat"
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy and not app.has_modal)
            assert answers == ["API key", "private-api-key", "deepseek-chat"]
            assert not session.main.messages and not faux.CALLS["test"]
            assert "private-api-key" not in str(session.store.branch())
            assert not app.history.path.exists()
            assert not app.transcript.query(".notice")

    asyncio.run(scenario())


def test_fullscreen_escape_interrupt_and_next_login(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        answers = []
        session.commands.register("testlogin", lambda s, _: answers.append(s.ui.input("Key", password=True)))
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            for key in ("escape", "ctrl+c"):
                app.controller.submit("/testlogin")
                await wait_for(pilot, lambda: isinstance(app.screen, InputScreen) and app.screen.is_mounted)
                app.screen.query_one(Input).value = "discard-me"
                await pilot.press(key)
                await wait_for(pilot, lambda: not app.controller.busy and not app.has_modal)
                assert answers[-1] is None
                assert not ui._inputs
            app.controller.submit("/testlogin")
            await wait_for(pilot, lambda: isinstance(app.screen, InputScreen) and app.screen.is_mounted)
            app.screen.query_one(Input).value = "new-value"
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert answers == [None, None, "new-value"]
            assert not session.main.messages

    asyncio.run(scenario())
