"""Authentication forms stay in the command dock and outside model input."""

from __future__ import annotations

import asyncio
import json
import threading

from textual import events
from textual.widgets import Input, OptionList

from virgo.ai.auth_presets import API_PRESETS
from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.harness.ui import UI

from tests.support.harness import wait_until
from tests.support.tui import form_is_ready, wait_for


def assert_inline_form(app, choices):
    assert len(app.screen_stack) == 1 and not app.has_modal
    assert app.query_one("#completions") is choices
    panel = app.query_one("#command-prompt")
    assert panel.region.x == 0 and panel.region.width == app.size.width
    assert panel.region.y >= app.transcript.region.bottom
    title = panel.query_one(".prompt-title")
    if title.display:
        assert str(title.content) == app.input_request.question
    else:
        assert panel.border_title == app.input_request.question
    field = choices if app.input_request.options is not None else app.query_one("#auth-value")
    assert panel.content_region.contains_region(field.region)
    assert panel.content_region.contains_region(app.query_one("#prompt-hints").region)
    status = app.query_one("#status")
    assert status.region.y == panel.region.bottom + 1 and status.region.bottom <= app.size.height


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
        wait_until(lambda ui=ui: bool(ui._inputs))
        cancel(ui)
        worker.join(1)
        assert answers == [None]
        assert not ui._inputs
        assert any(update.kind == "input_cancelled" for update in ui.drain())
    assert ui.input("Again") is None
    assert ui.select("Provider", ["one"]) is None


def test_inline_select_password_and_default_stay_out_of_history(make_session):
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
            choices = app.query_one("#completions", OptionList)
            app.controller.submit("/testlogin")
            await wait_for(pilot, lambda: form_is_ready(app, "Login method"))
            assert_inline_form(app, choices)
            assert choices.highlighted == 0
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: form_is_ready(app, "API key"))
            assert_inline_form(app, choices)
            field = app.query_one("#auth-value", Input)
            assert field.password and field.display and app.focused is field
            field.value = "private-api-key"
            await pilot.press("enter")
            await wait_for(pilot, lambda: form_is_ready(app, "Model"))
            assert_inline_form(app, choices)
            assert field.value == "deepseek-chat" and not field.password
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy and app.active_input is None)
            assert answers == ["API key", "private-api-key", "deepseek-chat"]
            assert not session.main.messages and not faux.CALLS["test"]
            assert "private-api-key" not in str(session.store.branch())
            assert not app.history.path.exists()
            assert not app.transcript.query(".notice")
            assert not field.value and not field.display
            assert app.focused is app.editor and app.editor.display

    asyncio.run(scenario())


def test_inline_escape_interrupt_and_next_login_preserve_draft(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        answers = []
        session.commands.register("testlogin", lambda s, _: answers.append(s.ui.input("Key", password=True)))
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.post_message(events.Paste("unfinished draft"))
            await wait_for(pilot, lambda: app.editor.text == "unfinished draft")
            for key in ("escape", "ctrl+c"):
                app.controller.submit("/testlogin")
                await wait_for(pilot, lambda: form_is_ready(app, "Key"))
                assert not app.has_modal
                app.query_one("#auth-value", Input).value = "discard-me"
                await pilot.press(key)
                await wait_for(pilot, lambda: not app.controller.busy and app.active_input is None)
                assert answers[-1] is None and not ui._inputs
                assert app.editor.text == "unfinished draft" and app.focused is app.editor
                assert not app.query_one("#auth-value", Input).value
            app.controller.submit("/testlogin")
            await wait_for(pilot, lambda: form_is_ready(app, "Key"))
            app.query_one("#auth-value", Input).value = "new-value"
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy and app.active_input is None)
            assert answers == [None, None, "new-value"]
            assert app.editor.text == "unfinished draft"
            assert not session.main.messages and not app.history.path.exists()

    asyncio.run(scenario())


def test_real_login_wizard_reuses_command_list_and_saves_key_without_history(make_session, monkeypatch):
    monkeypatch.setattr("virgo.harness.auth_commands.refresh_provider_catalog", lambda session, provider: None)
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.post_message(events.Paste("/login"))
            await wait_for(pilot, lambda: app.completion_names == ["/login"])
            choices = app.query_one("#completions", OptionList)
            await pilot.press("enter")
            await wait_for(pilot, lambda: form_is_ready(app, "Login method"))
            assert_inline_form(app, choices)
            assert choices.highlighted == 0 and choices.option_count == 2
            for width in (140, 36, 80):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                lines = [strip.text for strip in app.screen._compositor.render_strips()]
                status_y = app.query_one("#status").region.y
                top = next(i for i, line in enumerate(lines) if "Login method" in line)
                assert lines[top].startswith("╭") and lines[top].endswith("╮")
                assert lines[status_y - 2] == "╰" + "─" * (width - 2) + "╯"
                assert not lines[status_y - 1].strip()
                body = [line[1:-1].strip() for line in lines[top + 1:status_y - 2]]
                assert all(line.startswith("│") and line.endswith("│") for line in lines[top + 1:status_y - 2])
                assert "› Auth — Sign in to an account" in body
                assert "↑↓ select · Enter confirm · Esc cancel" in " ".join(body)
                assert not app.query_one("#hints").display
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: form_is_ready(app, "API — Choose a provider"))
            assert_inline_form(app, choices)
            assert choices.highlighted == 0 and choices.option_count == 4
            await pilot.press("enter")
            await wait_for(pilot, lambda: form_is_ready(app, "Base URL"))
            assert_inline_form(app, choices)
            field = app.query_one("#auth-value", Input)
            assert field.value == API_PRESETS["deepseek"]["config"]["baseUrl"]
            await pilot.press("enter")
            await wait_for(pilot, lambda: form_is_ready(app, "Model ID"))
            assert field.value == API_PRESETS["deepseek"]["model"]
            await pilot.press("enter")
            await wait_for(pilot, lambda: form_is_ready(app, "API key"))
            assert field.password
            app.post_message(events.Paste("sk-private-wizard-key"))
            await wait_for(pilot, lambda: field.value == "sk-private-wizard-key")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.controller.busy and app.active_input is None)
            assert session.auth.all()["deepseek"]["key"] == "sk-private-wizard-key"
            assert session.model.spec == f"deepseek/{API_PRESETS['deepseek']['model']}"
            assert not app.has_modal and app.editor.display
            assert not field.display and field.value == ""
            history = [json.loads(line) for line in app.history.path.read_text().splitlines()]
            assert history == ["/login"]
            assert not session.main.messages and not faux.CALLS["test"]
            assert "sk-private-wizard-key" not in str(session.store.branch())
            assert all("sk-private-wizard-key" not in str(row.content) for row in app.transcript.query(".notice"))

    asyncio.run(scenario())


def test_inline_form_remains_in_dock_when_terminal_resizes(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        question = "请输入用于本次会话的 API key，或者按 Esc 取消"
        session.commands.register("testlogin", lambda s, _: s.ui.input(question, password=True))
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            choices = app.query_one("#completions", OptionList)
            app.controller.submit("/testlogin")
            await wait_for(pilot, lambda: form_is_ready(app, question))
            for width, height in ((80, 24), (36, 16), (120, 40)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert_inline_form(app, choices)
                panel = app.query_one("#command-prompt")
                assert panel.query_one(".prompt-title").display == (width == 36)
                field = app.query_one("#auth-value", Input)
                assert field.region.bottom <= height and field.region.width <= width
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)

    asyncio.run(scenario())
