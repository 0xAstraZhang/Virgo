"""Plan UI acceptance through the real session, commands, and offline agent.

Failure cases: edits execute implicitly; an outdated editor overwrites a newer
plan or confirms its replacement; history replay starts work; mode shortcuts
change approval or sandbox; a dirty plan can execute without saving/reviewing.
"""

from __future__ import annotations

import asyncio
import threading

from textual.widgets import Button, Static, TextArea

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI

from tests.support.tui import has_reply, wait_for

PLAN = "# Update greeting\n\n## Goal\nUse the requested greeting.\n\n## Steps\nRead then edit greeting.txt."
REVISED = PLAN + "\n\n## Acceptance\nThe greeting must be bilingual."


def test_plan_editor_saves_revision_then_requires_explicit_execution(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("implementation requested"), ui=ui, mode="auto-review",
                               settings={"sandbox": {"mode": "read-only"}})
        session.prompt("/plan edit " + PLAN)
        original = session.services["plans"].current
        app = VirgoApp(session, ui)
        async with app.run_test(size=(100, 32)) as pilot:
            app.editor.load_text("unsent conversation draft")
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            assert app.screen.query_one("#plan-markdown", TextArea).text == PLAN
            assert original.token in str(app.screen.query_one("#plan-version", Static).content)
            assert not faux.CALLS["test"]
            editor = app.screen.query_one("#plan-markdown", TextArea)
            editor.load_text(REVISED)
            await pilot.pause()
            assert app.screen.query_one("#plan-execute", Button).disabled
            await pilot.click("#plan-save")
            await wait_for(pilot, lambda: not app.has_modal and not app.controller.busy)
            revised = session.services["plans"].current
            assert revised.markdown == REVISED and revised.token != original.token
            assert revised.confirmed_revision is None
            assert session.services["permissions"].work_mode == "plan"
            assert session.permission_mode == "auto-review"
            assert session.services["sandbox"].mode == "read-only"
            assert app.editor.text == "unsent conversation draft"
            assert not faux.CALLS["test"]
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            await pilot.click("#plan-execute")
            await wait_for(pilot, lambda: has_reply(app, "implementation requested") and not app.controller.busy)
            assert session.services["permissions"].work_mode == "default"
            assert session.services["plans"].current.confirmed_revision == revised.revision
            assert session.permission_mode == "auto-review"
            assert session.services["sandbox"].mode == "read-only"
            assert any(REVISED in message["content"] for message in faux.CALLS["test"][-1]["messages"]
                       if isinstance(message["content"], str))

    asyncio.run(scenario())


def test_stale_plan_view_cannot_execute_or_overwrite_replacement(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        session.prompt("/plan edit " + PLAN)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            session.prompt("/plan edit " + REVISED)
            await pilot.click("#plan-execute")
            await pilot.pause()
            assert not faux.CALLS["test"]
            assert session.services["permissions"].work_mode == "plan"
            assert session.services["plans"].current.markdown == REVISED
            if app.has_modal:
                await pilot.press("escape")
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            app.screen.query_one("#plan-markdown", TextArea).load_text(PLAN)
            session.prompt("/plan edit " + REVISED + "\nA newer constraint.")
            await pilot.click("#plan-save")
            await pilot.pause()
            assert session.services["plans"].current.markdown.endswith("A newer constraint.")
            assert not faux.CALLS["test"]

    asyncio.run(scenario())


def test_restored_plan_is_viewable_without_starting_execution(make_session):
    async def scenario():
        saved = make_session(persist=True)
        saved.prompt("/plan edit " + PLAN)
        path = saved.store.path
        saved.shutdown()
        ui = FullscreenUI()
        session = make_session(session=path, ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not app.has_modal and not app.controller.busy
            assert not faux.CALLS["test"]
            assert "Plan (read-only)" in str(app.query_one("#status", Static).content)
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            assert app.screen.query_one("#plan-markdown", TextArea).text == PLAN
            await pilot.press("escape")
            assert not faux.CALLS["test"]

    asyncio.run(scenario())


def test_busy_plan_execution_is_not_queued_by_modal(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        session.prompt("/plan edit " + PLAN)
        app = VirgoApp(session, ui)
        started, release = threading.Event(), threading.Event()
        session.commands.register("hold", lambda *_: (started.set(), release.wait(5)))
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("ctrl+g")
                await wait_for(pilot, lambda: app.has_modal)
                app.controller.submit("/hold")
                await wait_for(pilot, started.is_set)
                await pilot.click("#plan-execute")
                await pilot.pause()
                assert not app.controller.pending
                assert session.services["permissions"].work_mode == "plan"
                assert session.services["plans"].current.confirmed_revision is None
                release.set()
                await wait_for(pilot, lambda: not app.controller.busy)
                assert not faux.CALLS["test"]
        finally:
            release.set()

    asyncio.run(scenario())
