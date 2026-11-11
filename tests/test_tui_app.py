"""Exercise the assembled fullscreen frontend against the real offline harness."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from rich.cells import cell_len
from textual import events
from textual.color import Color
from textual.widgets import Static

from virgo.ai.providers import faux
from virgo.ai.types import UserMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import Confirmation, FullscreenUI
from virgo.cli.tui.dialogs import ApprovalScreen, SessionPicker
from virgo.cli.tui.transcript import AssistantView
from virgo.features import permission as permission_module
from virgo.features.permission import ReviewDecision


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
            await pilot.press("shift+enter", "Z")
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
            await wait_for(pilot, lambda: app.query_one("#preferences").display)
            assert not faux.CALLS["test"]
            await pilot.press("escape")
            await wait_for(pilot, lambda: app.active_input is None and not app.controller.busy)
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
            scroll_y = app.transcript.scroll_y
            await pilot.press("ctrl+end")
            assert app.transcript.scroll_y == scroll_y
            assert not app.transcript.follow_latest
            app.transcript.focus()
            await pilot.press("end", "ctrl+end")
            assert app.transcript.scroll_y == scroll_y
            assert not app.transcript.follow_latest
            assert await pilot.click("#jump")
            await wait_for(pilot, lambda: app.transcript.scroll_y == app.transcript.max_scroll_y)
            assert app.transcript.follow_latest
            await wait_for(pilot, lambda: not app.query_one("#jump").display)
            assert app.focused is app.editor

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
                assert app.query_one("#metrics").region.bottom == app.editor.region.y
                assert app.editor.region.bottom == app.query_one("#status").region.y
                assert app.query_one("#status").region.bottom <= height
                assert app.query_one("#hints").region.bottom <= height
                assert app.editor.region.width <= width
            assert app.editor.text.count("\n") == 29
            assert app.editor.styles.background.ansi == -1
            assert app.editor.styles.color.ansi == -1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("keys", "answer"),
    [
        (("y",), "y"),
        (("n",), "n"),
        (("escape",), "n"),
        (("a",), "a"),
        (("enter",), "n"),
        (("left", "enter"), "y"),
        (("right", "enter"), "a"),
        (("left", "left", "left", "enter"), "n"),
        (("right", "right", "right", "enter"), "n"),
    ],
)
def test_permission_answers_control_real_file_writes(make_session, project, monkeypatch, keys, answer):
    monkeypatch.setattr(permission_module, "review_tool_call", lambda *_: ReviewDecision("ask", "writes outside the workspace"))
    outside = project.parent / "outside"
    outside.mkdir()

    async def scenario():
        ui = FullscreenUI()
        calls = [("write", {"path": str(outside / "approved.txt"), "content": "first"})]
        if answer == "a":
            calls.append(("write", {"path": str(outside / "approved.txt"), "content": "first"}))
        session = make_session(faux.reply(tools=calls), faux.reply("finished"), ui=ui, mode="auto-review")
        app = VirgoApp(session, ui, initial="write files")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.focused is not None)
            assert app.focused.id == "deny"
            assert "approved.txt" in app.screen.confirmation.question
            assert not (outside / "approved.txt").exists()
            await pilot.press(*keys)
            await wait_for(pilot, lambda: has_reply(app, "finished") and not app.controller.busy)
            assert (outside / "approved.txt").exists() is (answer != "n")
            assert not app.has_modal
            if answer == "a":
                assert (outside / "approved.txt").read_text() == "first"
                assert session.services["permissions"].session_rules

    asyncio.run(scenario())


def test_permission_arrows_cycle_buttons_without_answering_or_focusing_question(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ApprovalScreen(Confirmation(1, "Long permission question\n" * 30, "main"))
            answers = []
            await app.push_screen(screen, answers.append)
            await pilot.pause()
            assert app.focused.id == "deny"
            question = screen.query_one(".question")
            assert question.max_scroll_y > 0
            for key, focused in (
                ("left", "allow"),
                ("left", "always"),
                ("left", "deny"),
                ("right", "always"),
                ("right", "allow"),
                ("right", "deny"),
            ):
                await pilot.press(key)
                assert app.focused.id == focused
                assert app.screen is screen and not answers
            question.focus()
            await pilot.press("down")
            await pilot.pause()
            assert question.scroll_y > 0
            await pilot.press("right")
            assert app.focused.id == "allow"
            await pilot.press("tab")
            assert app.focused.id == "deny"
            await pilot.press("shift+tab")
            assert app.focused.id == "allow"
            assert not answers
            await pilot.press("escape")
            assert answers == ["n"]

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
                await wait_for(pilot, lambda: "Closing services" in str(app.query_one("#hints", Static).content))
                assert "closing" not in str(app.query_one("#status", Static).content)
                release.set()
                await wait_for(pilot, lambda: app.controller.closed)
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize(("mode", "label"), [("auto-review", "auto review"), ("accept-all", "accept all"), ("plan", "plan")])
def test_status_preserves_fields_with_wide_path_and_long_model(make_session, project, mode, label):
    directory = project / ("中文项目" * 15)
    directory.mkdir()

    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, cwd=directory, mode=mode)
        session.main.model.id = "a-very-long-模型-name-" * 6
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for width in (80, 40, 52, 120):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                app.update_status()
                metrics = app.query_one("#metrics", Static)
                status = app.query_one("#status", Static)
                model_line = str(status.content)
                assert "\n" not in model_line
                assert cell_len(model_line) == width - 2
                assert model_line.startswith(label + "  ")
                assert status.render_line(0).text == model_line
                assert "ready" not in model_line
                assert str(metrics.content).startswith("↑0 ↓0")
                assert cell_len(str(metrics.content)) <= width - 2
                assert model_line.endswith(f" · {session.effort}")
                assert "faux/" not in model_line
                assert metrics.region.bottom == app.editor.region.y
                assert app.editor.region.bottom == status.region.y
                cwd = app.query_one("#cwd", Static)
                assert cell_len(str(cwd.content)) <= width - 2
                assert str(cwd.content).endswith(directory.name[-8:])
                assert cwd.region.y == app.query_one("#brand").region.bottom
                assert cwd.region.bottom == app.query_one("#hints").region.y
                assert app.query_one("#hints").region.bottom <= app.transcript.region.y
                assert str(directory) not in model_line

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal,newline", [("Apple_Terminal", "Ctrl+J"), ("xterm", "Shift+Enter")])
def test_status_places_usage_above_editor_and_permission_model_below(make_session, monkeypatch, terminal, newline):
    monkeypatch.setenv("TERM_PROGRAM", terminal)
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, mode="auto-review")
        session.main.model.provider = "deepseek"
        session.main.model.id = "deepseek-flash"
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.update_status()
            metrics = app.query_one("#metrics", Static)
            status = app.query_one("#status", Static)
            assert str(metrics.content).startswith("↑0 ↓0  ~0.0%/")
            session.store.append("custom", kind="model_call", data={
                "purpose": "permission", "usage": {"input": 12, "output": 3, "cache_read": 45},
            })
            app.update_status()
            assert str(metrics.content).startswith("↑12 ↓3 cache 45  ~0.0%/")
            assert metrics.styles.text_align == "right"
            assert metrics.render_line(0).text.endswith(str(metrics.content))
            assert metrics.render_line(0).text.startswith(" ")
            assert status.styles.text_align == "left"
            status_line = status.render_line(0).text
            assert status_line.startswith("auto review  ")
            assert status_line.endswith(f"deepseek-flash · {session.effort}")
            assert cell_len(status_line) == app.editor.region.width
            assert metrics.region.bottom == app.editor.region.y
            assert app.editor.region.bottom == status.region.y
            hint = app.query_one("#hints", Static).content
            assert f"{newline} newline" in str(hint)
            for key in ("Enter", newline, "Esc", "/", "Shift+Tab"):
                assert hint.get_style_at_offset(app.console, hint.plain.index(key)).bold
            for word in ("send", "newline", "interrupt", "commands", "mode"):
                assert not hint.get_style_at_offset(app.console, hint.plain.index(word)).bold
            assert not app.query_one("#jump").display

    asyncio.run(scenario())


def test_shift_tab_cycles_permission_modes_without_changing_draft(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, mode="auto-review")
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("unfinished task")
            for mode, label, color in (
                ("accept-all", "accept all", "#DC2626"),
                ("plan", "plan", "#2563EB"),
                ("auto-review", "auto review", "#B8860B"),
            ):
                await pilot.press("shift+tab")
                assert session.permission_mode == mode
                text = app.query_one("#status", Static).content
                assert text.plain.startswith(label + "  ")
                assert text.get_style_at_offset(app.console, 0).color.get_truecolor() == Color.parse(color).rgb
                assert app.editor.text == "unfinished task"
                assert app.focused is app.editor
            assert not faux.CALLS["test"]

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
