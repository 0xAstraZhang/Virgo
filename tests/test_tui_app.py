"""Exercise the assembled fullscreen frontend against the real offline harness."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from rich.cells import cell_len
from textual import events
from textual.widgets import Static

from virgo.ai.providers import faux
from virgo.ai.types import UserMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.dialogs import ApprovalScreen, SessionPicker
from virgo.cli.tui.transcript import AssistantView


async def wait_for(pilot, predicate, *, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "UI did not reach the expected state"
        await pilot.pause(0.01)


def has_reply(app, text):
    return any(
        card.text == text and card.body._markdown == text for card in app.transcript.query(AssistantView)
    )


def test_initial_prompt_and_chinese_multiline_paste_submit_once(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("初始回答"), faux.reply("收到多行输入"), ui=ui)
        app = VirgoApp(session, ui, initial="初始问题")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: has_reply(app, "初始回答") and not app.controller.busy)
            assert app.transcript.display
            assert len(app.transcript.query(".user-message")) == 1
            app.post_message(events.Paste("你好，世界\n第二行"))
            await wait_for(pilot, lambda: app.editor.text == "你好，世界\n第二行")
            await pilot.press("ctrl+j", "Z")
            assert app.editor.text == "你好，世界\n第二行\nZ"
            await pilot.press("enter")
            await wait_for(pilot, lambda: has_reply(app, "收到多行输入") and not app.controller.busy)
            assert app.editor.text == ""
            assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == [
                "初始问题",
                "你好，世界\n第二行\nZ",
            ]
            assert len(app.transcript.query(".user-message")) == 2
            history = [json.loads(line) for line in app.history.path.read_text().splitlines()]
            assert history[-1] == "你好，世界\n第二行\nZ"
            await pilot.press("up")
            assert app.editor.text == history[-1]

    asyncio.run(scenario())


def test_completion_tab_accepts_escape_closes_and_command_stays_local(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("/", "m", "o")
            await wait_for(pilot, lambda: app.completion_names == ["/model"])
            await pilot.press("tab")
            assert app.editor.text == "/model "
            assert not app.query_one("#completions").display
            assert not session.main.messages
            await pilot.press("enter")
            await wait_for(
                pilot,
                lambda: any("model:" in str(row.content) for row in app.transcript.query(".notice")),
            )
            assert not faux.CALLS["test"]
            await pilot.press("/", "e")
            await wait_for(pilot, lambda: bool(app.completion_names))
            await pilot.press("escape")
            assert not app.query_one("#completions").display
            assert app.editor.text == "/e"
            assert not app.controller.closing

    asyncio.run(scenario())


def test_exact_slash_command_submits_with_one_enter(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.post_message(events.Paste("/help"))
            await wait_for(pilot, lambda: app.completion_names == ["/help"])
            await pilot.press("enter")
            await wait_for(pilot, lambda: bool(app.transcript.query(".notice")))
            assert app.editor.text == "" and not faux.CALLS["test"]
            app.post_message(events.Paste("/exit"))
            await wait_for(pilot, lambda: app.completion_names == ["/exit"])
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.controller.closed)
            assert app.return_value == 0

    asyncio.run(scenario())


def test_up_moves_within_wrapped_line_before_recalling_history(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("remembered"), ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.post_message(events.Paste("first prompt"))
            await wait_for(pilot, lambda: app.editor.text == "first prompt")
            await pilot.press("enter")
            await wait_for(pilot, lambda: has_reply(app, "remembered") and not app.controller.busy)
            draft = "连续的中文输入" * 30
            app.post_message(events.Paste(draft))
            await wait_for(pilot, lambda: app.editor.text == draft)
            assert app.editor.document.line_count == 1
            assert app.editor.wrapped_document.height > 1
            before = app.editor.cursor_location
            await pilot.press("up")
            assert app.editor.text == draft
            assert app.editor.cursor_location[1] < before[1]

    asyncio.run(scenario())


def test_ctrl_c_interrupts_then_clears_input_and_exits_on_double_press(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("should be interrupted", delay=1), ui=ui)
        app = VirgoApp(session, ui, initial="slow request")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: session.main.agent.running)
            await pilot.press("ctrl+c")
            await wait_for(pilot, lambda: not app.controller.busy)
            assert any(
                getattr(message, "stop_reason", None) == "aborted" for message in session.main.messages
            )
            app.post_message(events.Paste("unfinished input"))
            await wait_for(pilot, lambda: app.editor.text == "unfinished input")
            await pilot.press("ctrl+c")
            assert app.editor.text == "" and not app.controller.closing
            await pilot.press("ctrl+c")
            assert not app.controller.closing
            await pilot.press("ctrl+c")
            await wait_for(pilot, lambda: app.controller.closed)
            assert app.return_value == 0

    asyncio.run(scenario())


def test_tool_and_thinking_shortcuts_and_scroll_navigation(make_session, project):
    (project / "sample.txt").write_text("one\ntwo\nthree\nfour\nfive\n")

    async def scenario():
        ui = FullscreenUI()
        session = make_session(
            faux.reply("Reading.", thinking="Check the file.", tools=[("read", {"path": "sample.txt"})]),
            faux.reply("\n\n".join(f"Paragraph {i}." for i in range(30))),
            ui=ui,
        )
        app = VirgoApp(session, ui, initial="read sample.txt")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(
                pilot, lambda: len(app.transcript.query(AssistantView)) == 2 and not app.controller.busy
            )
            await pilot.pause()
            tools = list(app.transcript.tools.values())
            thought = next(card for card in app.transcript.query(AssistantView) if card.thinking)
            assert tools and not any(tool.expanded for tool in tools)
            assert not thought.thought_body.display
            await pilot.press("ctrl+o", "ctrl+t")
            assert all(tool.expanded for tool in tools)
            assert thought.thought_body.display
            await pilot.press("ctrl+o", "ctrl+t")
            assert not any(tool.expanded for tool in tools)
            assert not thought.thought_body.display
            await pilot.press("ctrl+home")
            await wait_for(pilot, lambda: app.transcript.scroll_y == 0)
            assert not app.transcript.follow_latest
            await wait_for(pilot, lambda: app.query_one("#jump").display)
            await pilot.press("pagedown")
            assert app.transcript.scroll_y > 0
            await pilot.press("ctrl+end")
            await wait_for(pilot, lambda: app.transcript.scroll_y == app.transcript.max_scroll_y)
            assert app.transcript.follow_latest

    asyncio.run(scenario())


def test_layout_keeps_input_and_status_visible_after_resize(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("A short reply."), ui=ui)
        app = VirgoApp(session, ui, initial="hello")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: has_reply(app, "A short reply."))
            app.post_message(events.Paste("\n".join("很长的多行输入" for _ in range(30))))
            await wait_for(pilot, lambda: app.editor.document.line_count == 30)
            for width, height in ((80, 24), (120, 40), (80, 24)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert 3 <= app.editor.region.height <= height // 3
                assert app.transcript.region.bottom <= app.editor.region.y
                assert app.query_one("#status").region.bottom <= height
                assert app.query_one("#hints").region.bottom <= height
                assert app.editor.region.width <= width
            assert app.editor.text.count("\n") == 29
            assert app.editor.styles.background.ansi == -1
            assert app.editor.styles.color.ansi == -1

    asyncio.run(scenario())


@pytest.mark.parametrize(("key", "allowed"), [("y", True), ("n", False), ("escape", False), ("a", True)])
def test_permission_answers_control_real_file_writes(make_session, project, key, allowed):
    async def scenario():
        ui = FullscreenUI()
        calls = [("write", {"path": "approved.txt", "content": "first"})]
        if key == "a":
            calls.append(("write", {"path": "second.txt", "content": "second"}))
        session = make_session(faux.reply(tools=calls), faux.reply("finished"), ui=ui, mode="default")
        app = VirgoApp(session, ui, initial="write files")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.focused is not None)
            assert app.focused.id == "deny"
            assert "approved.txt" in app.screen.confirmation.question
            assert not (project / "approved.txt").exists()
            await pilot.press(key)
            await wait_for(pilot, lambda: has_reply(app, "finished") and not app.controller.busy)
            assert (project / "approved.txt").exists() is allowed
            assert not app.has_modal
            if key == "a":
                assert (project / "second.txt").read_text() == "second"
                assert session.services["permissions"].session_rules

    asyncio.run(scenario())


def test_exit_releases_displayed_and_queued_approvals(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        answers = {}
        workers = [
            threading.Thread(
                target=lambda name=name: answers.update({name: ui.confirm(f"Allow {name}?", name)}),
                daemon=True,
            )
            for name in ("main", "helper-one", "helper-two")
        ]
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                for worker in workers:
                    worker.start()
                await wait_for(
                    pilot, lambda: len(ui._waiting) == 3 and isinstance(app.screen, ApprovalScreen)
                )
                assert len(app.screen_stack) == 2
                await pilot.press("ctrl+d")
                await wait_for(pilot, lambda: app.controller.closed)
                await wait_for(pilot, lambda: len(answers) == 3)
                assert all(answer is None for answer in answers.values())
                assert not ui._waiting
                assert app.return_value == 0
        finally:
            ui.close()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(timeout=1)

    asyncio.run(scenario())


def test_slow_shutdown_keeps_ui_responsive(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        entered, release = threading.Event(), threading.Event()
        session.bus.on("session_shutdown", lambda _: (entered.set(), release.wait(2)))
        app = VirgoApp(session, ui)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("ctrl+d")
                await wait_for(pilot, entered.is_set)
                assert app.controller.closing and not app.controller.closed
                await pilot.resize_terminal(120, 40)
                await wait_for(pilot, lambda: app.size.width == 120)
                await wait_for(pilot, lambda: "closing" in str(app.query_one("#status", Static).content))
                release.set()
                await wait_for(pilot, lambda: app.controller.closed)
        finally:
            release.set()

    asyncio.run(scenario())


def test_status_preserves_fields_with_wide_path_and_long_model(make_session, project):
    directory = project / ("中文项目" * 15)
    directory.mkdir()

    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, cwd=directory)
        session.main.model.id = "a-very-long-model-name-" * 6
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for width in (80, 52, 120):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                app.update_status()
                lines = str(app.query_one("#status", Static).content).splitlines()
                assert len(lines) == (1 if width < 64 else 2)
                assert all(cell_len(line) <= width - 2 for line in lines)
                assert "ready" in lines[-1] and "yolo" in lines[-1]

    asyncio.run(scenario())


def test_ctrl_c_can_leave_ui_while_shutdown_hook_is_blocked(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        entered, release = threading.Event(), threading.Event()
        session.bus.on("session_shutdown", lambda _: (entered.set(), release.wait(4)))
        app = VirgoApp(session, ui)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("ctrl+d")
                await wait_for(pilot, entered.is_set)
                assert app.controller.closing and not app.controller.closed
                await pilot.press("ctrl+c")
                await wait_for(pilot, lambda: app.return_value == 130)
                assert not app.controller.closed
        finally:
            release.set()
            assert await asyncio.to_thread(app.controller.wait_closed, 2)

    asyncio.run(scenario())


def test_session_picker_restores_history_before_running_initial_prompt(make_session):
    saved = make_session(faux.reply("saved answer"), persist=True)
    saved.prompt("saved question")
    path = saved.store.path

    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("continued answer"), ui=ui)
        app = VirgoApp(session, ui, initial="continue here", pick_session=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert not faux.CALLS["test"]
            await pilot.press("enter")
            await wait_for(pilot, lambda: has_reply(app, "continued answer") and not app.controller.busy)
            assert session.store.path == path
            assert has_reply(app, "saved answer")
            assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == [
                "saved question",
                "continue here",
            ]
            assert len(app.transcript.query(".user-message")) == 2

    asyncio.run(scenario())


def test_empty_session_picker_can_cancel_without_model_call(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui, initial="must not run", pick_session=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert not app.screen.sessions
            await pilot.press("escape")
            await wait_for(pilot, lambda: app.controller.closed)
            assert app.return_value == 1
            assert not faux.CALLS["test"]

    asyncio.run(scenario())
