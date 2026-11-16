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
        session = make_session(faux.reply("implementation requested"), ui=ui, approval="ask", allow=False,
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
            assert session.main.active_plan is None
            assert session.services["permissions"].work_mode == "plan"
            assert session.approval_policy == "ask"
            assert session.services["sandbox"].mode == "read-only"
            assert app.editor.text == "unsent conversation draft"
            assert not faux.CALLS["test"]
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            await pilot.click("#plan-execute")
            await wait_for(pilot, lambda: has_reply(app, "implementation requested") and not app.controller.busy)
            assert session.services["permissions"].work_mode == "default"
            assert session.store.custom("plan_execution")[-1] == {"plan_id": revised.plan_id, "revision": revised.revision}
            assert session.main.active_plan is None
            assert session.approval_policy == "ask"
            assert session.services["sandbox"].mode == "read-only"
            assert faux.CALLS["test"][-1]["system"].count(REVISED) == 1

    asyncio.run(scenario())


def test_plan_editor_actions_remain_usable_after_compact_resize(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("compact plan executed"), ui=ui)
        session.prompt("/plan edit " + PLAN)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("unsent conversation draft")
            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            app.screen.query_one("#plan-markdown", TextArea).load_text(REVISED)
            await pilot.resize_terminal(36, 14)
            await pilot.press("tab")
            save = app.screen.query_one("#plan-save", Button)
            assert app.focused is save
            assert 0 <= save.region.y < save.region.bottom <= app.size.height
            assert await pilot.click("#plan-save", offset=(save.region.width // 2, save.region.height // 2))
            await wait_for(pilot, lambda: not app.has_modal and not app.controller.busy)
            proposal = session.services["plans"].current
            assert proposal.markdown == REVISED
            assert session.main.active_plan is None
            assert app.editor.text == "unsent conversation draft"
            assert not faux.CALLS["test"]

            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            await pilot.press("tab", "tab")
            close = app.screen.query_one("#plan-cancel", Button)
            assert app.focused is close
            assert 0 <= close.region.y < close.region.bottom <= app.size.height
            assert await pilot.click("#plan-cancel", offset=(close.region.width // 2, close.region.height // 2))
            await wait_for(pilot, lambda: not app.has_modal)
            assert not faux.CALLS["test"]

            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.has_modal)
            await pilot.press("tab")
            execute = app.screen.query_one("#plan-execute", Button)
            assert app.focused is execute
            assert 0 <= execute.region.y < execute.region.bottom <= app.size.height
            assert await pilot.click("#plan-execute", offset=(execute.region.width // 2, execute.region.height // 2))
            await wait_for(pilot, lambda: has_reply(app, "compact plan executed") and not app.controller.busy)
            assert session.store.custom("plan_execution")[-1] == {"plan_id": proposal.plan_id, "revision": proposal.revision}
            assert session.main.active_plan is None
            assert app.editor.text == "unsent conversation draft"
            await pilot.resize_terminal(80, 24)
            assert session.services["plans"].current.markdown == REVISED

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
                assert session.main.active_plan is None
                release.set()
                await wait_for(pilot, lambda: not app.controller.busy)
                assert not faux.CALLS["test"]
        finally:
            release.set()

    asyncio.run(scenario())
