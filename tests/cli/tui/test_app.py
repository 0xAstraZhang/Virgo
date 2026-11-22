"""Exercise the assembled fullscreen frontend against the real offline harness."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from rich.cells import cell_len
from textual import events
from textual.color import Color
from textual.geometry import Size
from textual.widgets import Button, OptionList, Static, TextArea

from virgo.agent.types import AgentEvent
from virgo.ai.providers import faux
from virgo.ai.stream import StreamEvent
from virgo.ai.types import AssistantMessage, TextContent, UserMessage
from virgo.cli.tui.app import VirgoApp, shortcut_hints
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.dialogs import ApprovalPicker, SessionPicker
from virgo.cli.tui.modes import PermissionModePicker
from virgo.cli.tui.transcript import AssistantView, ToolView
from virgo.i18n.locales.en.ui import MESSAGES as EN_UI
from virgo.i18n.locales.zh_CN.ui import MESSAGES as ZH_UI

from tests.support.tui import has_reply, wait_for


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
            history = [json.loads(line) for line in app.history.path.read_text(encoding="utf-8").splitlines()]
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
            await pilot.press("/", "m", "o", "d", "e", "l")
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


def test_alt_enter_and_backslash_continuation_submit_one_message(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("received"), ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test() as pilot:
            await pilot.press("a", "alt+enter", "b", "backslash", "enter")
            assert app.editor.text == "a\nb\n" and app.editor.cursor_location == (2, 0)
            await pilot.press("c", "backslash", "backslash", "backslash", "enter")
            assert app.editor.text == "a\nb\nc\\\\\n" and app.editor.cursor_location == (3, 0)
            assert not app.history.entries and not session.main.messages
            await pilot.press("d", "backslash", "backslash", "enter")
            await wait_for(pilot, lambda: has_reply(app, "received"))
            expected = "a\nb\nc\\\\\nd\\\\"
            assert app.history.entries == [expected]
            assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == [expected]

    asyncio.run(scenario())


def test_backslash_continuation_requires_end_of_line_and_empty_selection(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(faux.reply("first"), faux.reply("second"), ui=ui), ui)
        async with app.run_test() as pilot:
            editor = app.editor
            editor.load_text("a\\\nlater")
            editor.move_cursor((0, 2))
            await pilot.press("enter")
            assert editor.text == "a\n\nlater" and editor.cursor_location == (1, 0)
            editor.load_text("a\\b")
            editor.move_cursor((0, 2))
            await pilot.press("enter")
            await wait_for(pilot, lambda: has_reply(app, "first"))
            editor.load_text("a\\")
            editor.move_cursor((0, 1))
            await pilot.press("shift+right", "enter")
            await wait_for(pilot, lambda: has_reply(app, "second"))
            assert app.history.entries == ["a\\b", "a\\"]

    asyncio.run(scenario())


@pytest.mark.parametrize("language,placeholder", [
    ("en", "Ask anything, or type / for commands"), ("zh-CN", "输入消息，或输入 / 查看命令"),
])
def test_empty_editor_placeholder_preserves_cursor_text_and_history(make_session, language, placeholder):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui, settings={"language": language}), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.editor
            line = editor.render_line(0)
            assert editor.styles.border_top == editor.styles.border_bottom
            assert editor.styles.border_top[1].ansi == 8
            assert line.text.rstrip() == "›  " + placeholder
            assert all(segment.style is not None for segment in line)  # Required by Textual's monochrome filter.
            assert list(line.crop(2, 3)) == list(TextArea.render_line(editor, 0).crop(0, 1))
            assert any(seg.style and seg.style.color.name == "bright_black" for seg in line if seg.text == placeholder)
            await pilot.press("enter")
            app.set_focus(None)
            await pilot.pause()
            assert editor.render_line(0).text.rstrip() == "› " + placeholder
            await pilot.resize_terminal(16, 24)
            line = editor.render_line(0)
            assert line.cell_length == editor.content_size.width
            assert line.text.startswith("› ") and placeholder.startswith(line.text[2:].rstrip())
            assert editor.text == "" and app.history.entries == [] and not app.history.path.exists()
            assert not app.session.main.messages

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
                pilot, lambda: len(app.transcript.query(AssistantView)) == 1 and not app.controller.busy
            )
            await pilot.pause()
            tools = [t for t in app.transcript.query(ToolView) if t.kind == "tool_call"]
            thought = next(card for card in app.transcript.query(AssistantView)
                           if any(e.kind == "thinking" for e in card.events))
            assert tools and not any(tool.expanded for tool in tools)
            assert thought.thought_body.display
            assert all(not e.output.display for e in thought.events if e.kind == "thinking")
            await pilot.press("ctrl+o", "ctrl+t")
            assert all(tool.expanded for tool in tools)
            assert thought.thought_body.display
            assert all(e.output.display for e in thought.events if e.kind == "thinking")
            await pilot.press("ctrl+o", "ctrl+t")
            assert not any(tool.expanded for tool in tools)
            assert thought.thought_body.display
            assert all(not e.output.display for e in thought.events if e.kind == "thinking")
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
                status = app.query_one("#status").region
                meta = app.query_one("#footer-meta").region
                assert not app.query_one("#hints").display
                assert status.height == meta.height == 1
                assert app.editor.region.bottom == status.y
                assert status.bottom == meta.y and meta.bottom == height
                assert app.editor.region.width <= width
            assert app.editor.text.count("\n") == 29
            assert app.editor.styles.background.ansi == -1
            assert app.editor.styles.color.ansi == -1

    asyncio.run(scenario())


@pytest.mark.parametrize("keys,answer", [
    (("a",), "a"), (("y",), "y"), (("n",), "n"), (("escape",), "n"),
    (("1",), "y"), (("2",), "n"), (("enter",), "n"), (("up", "enter"), "y"),
], ids=["always-a", "once-y", "once-n", "once-escape", "once-1", "once-2", "once-enter", "once-up"])
def test_resource_approval_controls_one_file_write(broker_session, project, keys, answer):
    target = project.parent / "approved.txt"
    async def scenario():
        ui = FullscreenUI()
        args = {"path": str(target), "content": "first"}
        request = ("request_permissions", {"tool": "write", "arguments": args,
                   "permissions": {"write_paths": [str(target)]}, "reason": "Write requested result"})
        session = broker_session(faux.reply(tools=[request]), faux.reply(tools=[("write", args)]),
                                 faux.reply("finished"), ui=ui)
        app = VirgoApp(session, ui, initial="write file")
        async with app.run_test(size=(80, 24)) as pilot:
            picker = app.query_one(ApprovalPicker)
            await wait_for(pilot, lambda: picker.display and app.focused is not None)
            assert picker.query_one("#approval-options", OptionList).option_count == 3
            assert "approved.txt" in picker.confirmation.question and not target.exists()
            await pilot.press(*keys)
            await wait_for(pilot, lambda: has_reply(app, "finished") and not app.controller.busy)
            assert target.exists() is (answer in {"y", "a"})
            if answer == "a":
                from virgo.harness.resource_rules import ResourceRules

                assert ResourceRules(session.paths.user).list(str(session.cwd))
            assert not picker.display and app.focused is app.editor
            assert bool(session.services["execution"].sandbox._session_grants) is (answer in {"y", "a"})
    asyncio.run(scenario())


def test_permission_picker_keeps_transcript_and_draft_and_wraps_selection(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("Earlier response."), ui=ui,
                               settings={"execution": {"mode": "no-limit"}})
        app = VirgoApp(session, ui, initial="hello")
        answers = []
        worker = threading.Thread(
            target=lambda: answers.append(ui.confirm("Long permission question\n" * 30)),
            daemon=True,
        )
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await wait_for(pilot, lambda: has_reply(app, "Earlier response.") and not app.controller.busy)
                draft = "Keep this draft\n第二行"
                app.editor.load_text(draft)
                original_mode = session.approval_policy
                worker.start()
                picker = app.query_one(ApprovalPicker)
                await wait_for(pilot, lambda: picker.display)
                await pilot.pause()
                choices = picker.query_one("#approval-options", OptionList)
                question = picker.query_one("#approval-question")
                assert len(app.screen_stack) == 1
                assert app.transcript.display and has_reply(app, "Earlier response.")
                assert not app.editor.display
                assert app.query_one("#status").display
                assert app.editor.prompt_marker == "›"
                assert "No-Limit · Waiting for approval" in app.query_one("#status").render_line(0).text
                assert not app.query_one("#footer-meta").display and not app.query_one("#hints").display
                assert choices.highlighted == 1 and question.max_scroll_y > 0
                assert choices.get_option_at_index(1).prompt.plain.startswith("› 2. Deny")
                highlight = choices.get_component_styles("option-list--option-highlighted")
                assert highlight.color == Color.parse("#7C3AED")
                assert highlight.text_style.bold and not highlight.text_style.reverse
                for key, selected in (
                    ("up", 0), ("up", 2), ("up", 1),
                    ("down", 2), ("down", 0), ("down", 1),
                    ("left", 0), ("right", 1),
                ):
                    await pilot.press(key)
                    assert choices.highlighted == selected
                    assert [option.prompt.plain[:2] for option in choices.options] == [
                        "› " if i == selected else "  " for i in range(3)
                    ]
                    assert picker.display and not answers
                await pilot.press("pagedown")
                await pilot.pause()
                assert question.scroll_y > 0
                await pilot.press("ctrl+r", "shift+tab")
                assert isinstance(app.screen, SessionPicker) and session.approval_policy == original_mode
                assert not answers and app.screen.current_status == "Waiting"
                await pilot.press("escape")
                assert not app.has_modal and picker.display and not answers
                assert app.focused is choices
                for width, height in ((40, 16), (120, 40), (80, 24)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    assert app.transcript.region.height > 0
                    assert picker.region.bottom <= height
                    assert choices.region.bottom <= height
                    assert choices.region.width <= width
                    assert app.editor.text == draft
                choices.focus()
                await pilot.press("escape")
                await wait_for(pilot, lambda: answers == ["n"] and not picker.display)
                assert app.editor.display and app.focused is app.editor
                assert app.editor.text == draft
        finally:
            ui.close()
            if worker.ident is not None:
                worker.join(timeout=1)

    asyncio.run(scenario())


def test_single_operation_approval_keeps_permissions_visible_without_session_choice(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, settings={"execution": {"mode": "no-limit"}})
        app = VirgoApp(session, ui)
        answers = []
        worker = threading.Thread(target=lambda: answers.append(ui.confirm_once("Allow this exact write?")))
        try:
            async with app.run_test(size=(36, 14)) as pilot:
                await pilot.pause()
                worker.start()
                picker = app.query_one(ApprovalPicker)
                await wait_for(pilot, lambda: picker.display)
                choices = picker.query_one("#approval-options", OptionList)
                assert choices.option_count == 2
                assert choices.highlighted == 1
                status = app.query_one("#status", Static)
                assert status.display and status.region.bottom <= app.size.height
                assert app.editor.prompt_marker == "›"
                assert "No-Limit · Waiting for approval" in status.render_line(0).text
                await pilot.press("a", "3")
                assert not answers and picker.display
                await pilot.press("y")
                await wait_for(pilot, lambda: answers == ["y"])
                assert session.services["execution"].selected_mode_id == "no-limit"
        finally:
            ui.close()
            if worker.ident is not None:
                worker.join(1)

    asyncio.run(scenario())


def test_cancelled_inline_approval_advances_queue_and_ignores_stale_answer(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        answers = {}
        workers = [
            threading.Thread(
                target=lambda name=name: answers.update({name: ui.confirm(f"Allow {name}?", name)}),
                daemon=True,
            )
            for name in ("main", "helper")
        ]
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                picker = app.query_one(ApprovalPicker)
                workers[0].start()
                await wait_for(pilot, lambda: picker.display)
                first = picker.confirmation
                workers[1].start()
                await wait_for(pilot, lambda: len(app.state.confirmations) == 1)
                assert ui.resolve_confirmation(first.id, None)
                await wait_for(pilot, lambda: picker.confirmation is not None and picker.confirmation.agent == "helper")
                second = picker.confirmation
                assert len(app.screen_stack) == 1
                picker.post_message(ApprovalPicker.Answered(first, "y"))
                await pilot.pause()
                assert app.active_confirmation == second.id and ui.is_confirmation_pending(second.id)
                assert answers == {"main": None}
                await pilot.press("y")
                await wait_for(pilot, lambda: answers == {"main": None, "helper": "y"})
                assert not picker.display and app.active_confirmation is None
                assert app.focused is app.editor
        finally:
            ui.close()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(timeout=1)

    asyncio.run(scenario())


@pytest.mark.parametrize("approval_first", [False, True])
def test_inline_approval_and_input_prompts_are_serialized(make_session, approval_first):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        answers = {}
        workers = {
            "approval": threading.Thread(
                target=lambda: answers.update(approval=ui.confirm("Allow helper?", "helper")), daemon=True,
            ),
            "input": threading.Thread(
                target=lambda: answers.update(input=ui.select("Pick one", ["first", "second"])), daemon=True,
            ),
        }
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                picker = app.query_one(ApprovalPicker)
                first, second = ("approval", "input") if approval_first else ("input", "approval")
                modes = app.query_one(PermissionModePicker)
                await pilot.press("shift+tab", "down")
                assert modes.display
                original_mode = app.session.services["execution"].selected_mode_id
                workers[first].start()
                await wait_for(pilot, lambda: app.active_confirmation is not None or app.active_input is not None)
                assert not modes.display
                app.action_permission_modes()
                assert not modes.display
                workers[second].start()
                await wait_for(pilot, lambda: bool(app.state.input_requests if approval_first else app.state.confirmations))
                assert not app.has_modal
                assert picker.display is approval_first
                assert app.query_one("#completions").display is not approval_first
                assert (app.active_input is None) is approval_first
                assert (app.active_confirmation is not None) is approval_first
                await pilot.press("n" if approval_first else "enter")
                await wait_for(
                    pilot,
                    lambda: first in answers and (
                        app.active_input is not None if approval_first else app.active_confirmation is not None
                    ),
                )
                assert not app.has_modal
                await pilot.press("enter" if approval_first else "n")
                await wait_for(pilot, lambda: answers == {"approval": "n", "input": "first"})
                assert app.active_input is None and app.active_confirmation is None
                assert app.session.services["execution"].selected_mode_id == original_mode
                assert not picker.display and app.focused is app.editor
        finally:
            ui.close()
            for worker in workers.values():
                if worker.ident is not None:
                    worker.join(timeout=1)

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
                app.editor.load_text("Preserve this hidden draft")
                for worker in workers:
                    worker.start()
                await wait_for(
                    pilot, lambda: len(ui._waiting) == 3 and app.query_one(ApprovalPicker).display
                )
                assert len(app.screen_stack) == 1
                await pilot.press("ctrl+d")
                await wait_for(pilot, lambda: app.controller.closed)
                await wait_for(pilot, lambda: len(answers) == 3)
                assert all(answer is None for answer in answers.values())
                assert not ui._waiting
                assert app.return_value == 0
                assert app.editor.text == "Preserve this hidden draft"
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


def test_status_preserves_fields_with_wide_path_and_long_model(broker_session, project):
    directory = project / ("中文项目" * 15)
    directory.mkdir()

    async def scenario():
        ui = FullscreenUI()
        session = broker_session(ui=ui, cwd=directory)
        session.main.model.id = "a-very-long-模型-name-" * 6
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for width in (80, 36, 40, 52, 64, 120):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                app.update_status()
                metrics = app.query_one("#metrics", Static)
                status = app.query_one("#status", Static)
                model_line = str(status.content)
                flags_widget = app.query_one("#flags", Static)
                flags = str(flags_widget.content)
                assert cell_len(model_line) == width - 2
                authority = session.services["execution"].selected_mode.name
                assert model_line.startswith(authority[:10])
                if width - 2 >= cell_len(authority):
                    assert model_line.startswith(authority)
                assert status.render_line(0).text == model_line
                assert ("R:1 W:1 Web:on Shell:on" in flags or
                        "Read paths 1 · Write paths 1 · Web on · Shell net on" in flags)
                assert cell_len(flags) <= width - 2
                assert flags_widget.render_line(0).text.rstrip() == flags
                assert status.region.bottom <= app.size.height
                assert "ready" not in model_line
                if width >= 64:
                    assert metrics.display and metrics.render_line(0).text.startswith("↑0 ↓0 cache")
                if metrics.display:
                    assert metrics.render_line(0).text.rstrip() == str(metrics.content)
                    assert metrics.region.x > flags_widget.region.x + cell_len(flags) - 1
                if width - 2 - cell_len(authority) - 2 >= cell_len(f" · {session.effort}") + 1:
                    assert model_line.endswith(f" · {session.effort}")
                assert "faux/" not in model_line
                assert app.query_one("#footer-meta").region.y == status.region.bottom
                assert app.editor.region.bottom == status.region.y
                cwd = app.query_one("#cwd", Static)
                assert cell_len(str(cwd.content)) <= cwd.region.width
                assert str(cwd.content).startswith("…") and str(cwd.content).endswith(directory.name[-7:])
                assert cwd.render_line(0).text.rstrip().endswith(str(cwd.content))
                assert cwd.region.y == app.query_one("#brand").region.y == 0
                assert app.query_one("#topbar").region.height == app.transcript.region.y == 1
                assert str(directory) not in model_line

    asyncio.run(scenario())


def test_topbar_renders_correct_cwd_immediately_on_shrink_and_grow(make_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        path = PurePosixPath("/" + "long-workspace-" * 6 + "project")
        monkeypatch.setattr(app.session, "cwd", path)
        async with app.run_test(size=(80, 24)) as pilot:
            label = app.query_one("#cwd", Static)
            for width in (80, 60, 80):
                available = width - 3 - app.query_one("#brand", Static).content.cell_len
                expected = "…" + str(path)[-(available - 1):]
                # A terminal resize can reach the app before the child layout catches up.
                with monkeypatch.context() as patch:
                    patch.setattr(app, "_size", Size(width, 24))
                    app.update_status()
                    assert str(label.content) == expected
                await pilot.resize_terminal(width, 24)
                assert str(label.content) == expected
                assert label.render_line(0).text == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("home", [PurePosixPath("/home/alex"), PureWindowsPath("C:/Users/Alex")])
def test_topbar_abbreviates_only_the_home_directory(make_session, monkeypatch, home):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            label = app.query_one("#cwd", Static)
            for cwd, expected in (
                (home, "~"),
                (home / "src" / "Virgo", str(type(home)("~/src/Virgo"))),
                (home.with_name(home.name + "2"), str(home.with_name(home.name + "2"))),
            ):
                with monkeypatch.context() as patch:
                    patch.setattr(Path, "home", lambda: home)
                    patch.setattr(session, "cwd", cwd)
                    app.update_status()
                    assert label.content.no_wrap
                    assert label.render_line(0).text == expected.rjust(label.size.width)

    asyncio.run(scenario())


def test_shortcut_hints_style_the_entire_shortcut():
    keys = "Ctrl+O/T Ctrl+O Ctrl+T Ctrl+R Shift+Tab Shift+Enter Ctrl+J Enter Esc / /help /exit Backspace ↑↓ ←→ s".split()
    hint = shortcut_hints(" · ".join(keys))
    assert [hint.plain[span.start:span.end] for span in hint.spans if span.style == "bold"] == keys


def test_ui_locale_keys_match():
    assert EN_UI.keys() == ZH_UI.keys()


@pytest.mark.parametrize("language,messages", [("en", EN_UI), ("zh-CN", ZH_UI)])
@pytest.mark.parametrize("size", [(60, 18), (40, 16)])
def test_narrow_footer_renders_complete_idle_and_busy_shortcuts(make_session, monkeypatch, language, messages, size):
    monkeypatch.setenv("TERM_PROGRAM", "xterm")

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui, settings={"language": language}), ui)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            hints = app.query_one("#hints", Static)
            assert not hints.display

            app.controller.busy = True
            app.update_status()
            await pilot.pause()
            assert hints.display and hints.render_line(0).text.rstrip() == messages["ui.hints.busy"]

    asyncio.run(scenario())


def test_chinese_footer_fits_both_flags_and_usage_at_64_columns(broker_session):
    async def scenario():
        ui = FullscreenUI()
        session = broker_session(ui=ui, settings={"language": "zh-CN"})
        app = VirgoApp(session, ui)
        async with app.run_test(size=(64, 24)) as pilot:
            await pilot.pause()
            flags = app.query_one("#flags")
            metrics = app.query_one("#metrics")
            hints = app.query_one("#hints")
            assert flags.render_line(0).text.rstrip() == "读:1 写:1 网页开 Shell:开"
            assert metrics.render_line(0).text == str(metrics.content) and "缓存" in str(metrics.content)
            assert not hints.display

    asyncio.run(scenario())


def test_footer_places_permissions_and_usage_below_editor(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, settings={"execution": {"mode": "no-limit"}})
        session.main.model.provider = "deepseek"
        session.main.model.id = "deepseek-flash"
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.update_status()
            metrics = app.query_one("#metrics", Static)
            status = app.query_one("#status", Static)
            assert str(metrics.content).startswith("↑0 ↓0 cache 0.0%  0.0%/")
            assert session.context_tokens() > 0
            assert "~" not in str(metrics.content)
            session.store.append("custom", kind="model_call", data={
                "purpose": "permission", "usage": {"input": 12, "output": 3, "cache_read": 45, "cache_write": 3},
            })
            app.update_status()
            assert str(metrics.content).startswith("↑60 ↓3 cache 75.0%  0.0%/")
            session.store.append("custom", kind="model_call", data={
                "purpose": "agent", "usage": {"input": 40, "output": 7},
            })
            app.update_status()
            assert str(metrics.content).startswith("↑100 ↓10 cache 45.0%  0.0%/")
            assert metrics.styles.text_align == "right"
            await pilot.pause()
            assert metrics.render_line(0).text == str(metrics.content)
            assert metrics.region.right == app.editor.region.right
            assert status.styles.text_align == "left"
            status_line = status.render_line(0).text
            assert status_line.startswith("No-Limit  ")
            assert status_line.endswith(f"deepseek-flash · {session.effort}")
            assert "Host · Full OS access" in app.query_one("#flags").render_line(0).text
            assert cell_len(status_line) == app.editor.region.width
            assert metrics.region.y == status.region.bottom
            assert app.editor.region.bottom == status.region.y
            assert not app.query_one("#hints").display
            assert not app.query_one("#jump").display

    asyncio.run(scenario())


def test_shift_tab_cycles_permission_modes_without_changing_draft(broker_session):
    async def scenario():
        ui = FullscreenUI()
        session = broker_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("/mo")
            app.editor.move_cursor((0, 2))
            await wait_for(pilot, lambda: bool(app.completion_names))
            manager = session.services["execution"]
            panel = app.query_one(PermissionModePicker)
            picker = panel.query_one("#modes-list", OptionList)
            modes = list(manager.modes)
            await app.transcript.add_notice("Earlier conversation")
            for number, mode in enumerate(modes, 1):
                original = manager.selected_mode_id
                await pilot.press("shift+tab")
                assert panel.display and len(app.screen_stack) == 1 and not panel.query(Button)
                assert app.focused is picker and picker.highlighted == modes.index(original)
                assert picker.get_option(original).prompt.plain.startswith("› ")
                assert " ✓" in picker.get_option(original).prompt.plain
                assert "Host" in picker.get_option("no-limit").prompt.plain
                assert picker.size.height == picker.option_count == 3
                assert app.transcript.region.height > 0 and app.transcript.region.bottom <= panel.region.y
                assert app.query_one("#status").display
                hidden = ("#editor", "#footer-meta", "#hints", "#jump", "#completions")
                assert all(not app.query_one(selector).display for selector in hidden)
                for _ in modes:
                    last = picker.highlighted
                    await pilot.press("shift+tab")
                    assert picker.highlighted == (last + 1) % len(modes)
                    assert picker.get_option_at_index(picker.highlighted).prompt.plain.startswith("› ")
                    assert manager.selected_mode_id == original
                await pilot.press(str(number))
                await wait_for(pilot, lambda mode=mode: manager.selected_mode_id == mode and not panel.display)
                assert session.approval_policy == "ask"
                text = app.query_one("#status", Static).content
                assert text.plain.startswith(manager.selected_mode.name)
                assert app.editor.text == "/mo" and app.editor.cursor_location == (0, 2)
                assert app.focused is app.editor
                assert app.query_one("#completions").display

            await pilot.press("shift+tab", "right", "enter")
            await wait_for(pilot, lambda: manager.selected_mode_id == modes[0] and not panel.display)
            for cancel in ("escape", "ctrl+c"):
                await pilot.press("shift+tab", "tab", cancel)
                assert not panel.display and app.focused is app.editor
                assert manager.selected_mode_id == modes[0]
                assert app.editor.text == "/mo" and app.editor.cursor_location == (0, 2)

            await pilot.press("shift+tab", "g")
            await wait_for(pilot, lambda: any(
                "No saved grants" in str(row.content) for row in app.transcript.query(".notice")
            ))
            assert not panel.display and manager.selected_mode_id == modes[0]
            await pilot.press("shift+tab", "ctrl+r")
            assert isinstance(app.screen, SessionPicker) and not panel.display
            await pilot.press("escape")
            assert app.focused is app.editor and app.editor.text == "/mo"
            assert not faux.CALLS["test"]

    asyncio.run(scenario())


@pytest.mark.parametrize("language,wide_help,short_help", [
    ("en", "g saved approvals", "↑↓/1-3 · Enter · g grants · Esc"),
    ("zh-CN", "g 已保存授权", "↑↓/1-3 · Enter · g 授权 · Esc"),
])
def test_mode_picker_rows_and_keyboard_hints_fit_after_resize(make_session, language, wide_help, short_help):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui, settings={"language": language}), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("shift+tab")
            panel = app.query_one(PermissionModePicker)
            choices = panel.query_one(OptionList)
            help_line = panel.query_one("#modes-help", Static)
            for width, height in ((80, 24), (40, 16), (80, 24)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert choices.size.height == choices.option_count == 3
                assert "Host" in choices.render_line(choices.get_option_index("no-limit")).text
                assert (wide_help if width == 80 else short_help) in help_line.render_line(0).text
                assert help_line.size.height == 1
                assert app.transcript.region.height > 0 and app.query_one("#status").region.bottom <= height
            highlight = choices.get_component_styles("option-list--option-highlighted")
            assert highlight.color == Color.parse("#7C3AED") and highlight.text_style.bold
            assert not highlight.text_style.reverse
            assert app.check_action("permission_modes", ())
            disabled = ("page", "top", "tools", "thinking", "cancel_pending")
            assert all(not app.check_action(action, ()) for action in disabled)

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


def test_transcript_has_no_scrollbar_gutter_but_still_scrolls(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.transcript
            await transcript.add_notice("\n".join(f"Line {i}" for i in range(100)))
            await pilot.pause()
            assert transcript.max_scroll_y > 0
            assert transcript.scrollbar_size_vertical == 0
            assert transcript.scrollable_content_region.width == transcript.content_region.width
            await pilot.press("ctrl+home")
            await pilot.pause()
            assert transcript.scroll_y == 0
            await pilot.press("pagedown")
            await pilot.pause()
            assert transcript.scroll_y > 0
            assert not transcript.follow_latest
            await pilot.click("#jump")
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.follow_latest
            await pilot.resize_terminal(42, 16)
            await pilot.pause()
            assert transcript.scrollbar_size_vertical == 0
            assert transcript.scrollable_content_region.width == transcript.content_region.width

    asyncio.run(scenario())


@pytest.mark.parametrize("wheel", [False, True], ids=["page", "mouse-wheel"])
def test_manual_scroll_reanchors_at_bottom_during_stream(make_session, wheel):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.transcript
            await view.add_notice("\n".join(f"Line {i}" for i in range(60)))
            await wait_for(pilot, lambda: view.scroll_y == view.max_scroll_y > 0)

            async def scroll(direction):
                if wheel:
                    event = events.MouseScrollDown if direction > 0 else events.MouseScrollUp
                    view.post_message(event(view, 2, 2, 0, direction, 0, False, False, False))
                else:
                    await pilot.press("pagedown" if direction > 0 else "pageup")
                await pilot.pause()

            await scroll(-1)
            await wait_for(pilot, lambda: app.query_one("#jump").display)
            manual_y, old_max = view.scroll_y, view.max_scroll_y
            assert not view.follow_latest and manual_y < old_max
            partial = AssistantMessage([TextContent("First paragraph.\n\nSecond paragraph.")])
            await view.consume(AgentEvent("message_update", stream_event=StreamEvent("text_delta", partial)))
            await wait_for(pilot, lambda: view.max_scroll_y > old_max)
            assert not view.follow_latest and view.scroll_y == manual_y
            while not view.follow_latest:
                previous = view.scroll_y
                await scroll(1)
                assert view.scroll_y > previous or view.follow_latest
            await wait_for(pilot, lambda: not app.query_one("#jump").display)
            await view.consume(AgentEvent("message_end", message=partial))
            await pilot.pause()
            assert view.scroll_y == view.max_scroll_y and view.follow_latest

    asyncio.run(scenario())


def test_context_usage_stays_zero_until_submit_and_resets_on_new(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("done"), ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            metrics = app.query_one("#metrics", Static)
            assert "  0.0%/" in str(metrics.content)
            app.editor.load_text("unsent draft")
            await pilot.pause()
            assert "  0.0%/" in str(metrics.content)
            assert not faux.CALLS["test"]
            await pilot.press("enter")
            await wait_for(pilot, lambda: has_reply(app, "done") and not app.controller.busy)
            app.update_status()
            assert "  ~" in str(metrics.content)
            assert session.main.messages
            app.editor.load_text("/new")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not session.main.messages and not app.controller.busy)
            app.update_status()
            assert "  0.0%/" in str(metrics.content)

    asyncio.run(scenario())


def test_status_shows_active_preset_while_only_a_child_is_queued(broker_session):
    ui = FullscreenUI()
    session = broker_session(ui=ui)

    async def scenario():
        app = VirgoApp(session, ui)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            release = session.reserve_child_work(session.main.origin.root)
            try:
                assert not app.controller.busy
                await pilot.press("shift+tab", "1")
                await wait_for(pilot, lambda: "Next: Read-Only" in str(app.query_one("#status", Static).content))
                text = str(app.query_one("#status", Static).content).splitlines()[0]
                assert text.startswith("Workspace-Edit") and "Next: Read-Only" in text
            finally:
                release()

    asyncio.run(scenario())
