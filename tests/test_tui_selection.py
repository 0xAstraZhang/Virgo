"""Mouse selection reaches the real clipboard path; Enter stays distinct from newline."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input
from textual.widgets._toast import Toast

from virgo.agent.types import AgentEvent, ToolResult
from virgo.ai.types import AssistantMessage, TextContent, ThinkingContent, UserMessage
from virgo.cli.tui import selection as selection_module
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.editor import ChatEditor, InputHistory
from virgo.cli.tui.selection import SelectionCopyMixin
from virgo.cli.tui.transcript import AssistantView, Transcript

from .test_tui_pty import PtyClient, plain


class CopyApp(SelectionCopyMixin, App):
    def __init__(self, directory):
        super().__init__()
        self.history = InputHistory(directory)

    def compose(self) -> ComposeResult:
        yield Transcript()
        yield ChatEditor(self.history)
        yield Input("secret-value", password=True, id="secret")


def mock_mac_clipboard(monkeypatch):
    copied = []
    monkeypatch.setattr(selection_module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)

    def run(command, **kwargs):
        assert command == ["/usr/bin/pbcopy"]
        assert kwargs["timeout"] == 1 and kwargs["check"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        copied.append(kwargs["input"].decode("utf-8"))

    monkeypatch.setattr(selection_module.subprocess, "run", run)
    return copied


@pytest.mark.parametrize("gesture", ["drag", "double_click"])
def test_completed_selections_copy_user_answer_thinking_and_tool_text(monkeypatch, tmp_path, gesture):
    copied = mock_mac_clipboard(monkeypatch)

    async def scenario():
        app = CopyApp(tmp_path)
        async with app.run_test(size=(100, 42)) as pilot:
            transcript = app.query_one(Transcript)
            await transcript.consume(AgentEvent("message_end", message=UserMessage("中文 user words")))
            await transcript.consume(
                AgentEvent(
                    "message_end",
                    message=AssistantMessage(
                        [ThinkingContent("private reasoning"), TextContent("answer words")]
                    ),
                )
            )
            transcript.toggle_thinking()
            await transcript.consume(
                AgentEvent(
                    "tool_execution_end",
                    tool_call_id="tool",
                    tool_name="read",
                    result=ToolResult.text("tool words"),
                )
            )
            await pilot.pause()
            card = transcript.query_one(AssistantView)
            thought = next(event for event in card.events if event.kind == "thinking")
            thought.set_expanded(True)
            result = transcript.results[("main", "tool")]
            result.set_expanded(True)
            await pilot.pause()
            cases = [
                (transcript.query_one(".user-message"), "中文 user words"),
                (card.body.query_one("MarkdownParagraph"), "answer words"),
                (thought.output, "private reasoning"),
                (result.output, "tool words"),
            ]
            for widget, expected in cases:
                before = len(copied)
                if gesture == "drag":
                    await pilot.mouse_down(widget, offset=(0, 0))
                    assert len(copied) == before
                    await pilot.mouse_up(widget, offset=(min(widget.size.width - 1, 30), 0))
                else:
                    await pilot.double_click(widget, offset=(3, 0))
                await pilot.pause()
                assert len(copied) == before + 1
                assert expected in copied[-1]
                assert app.clipboard == copied[-1]
            before = len(copied)
            await pilot.click(card.thought_body, offset=(3, 0))
            assert len(copied) == before
            secret = app.query_one("#secret", Input)
            await pilot.mouse_down(secret, offset=(1, 1))
            await pilot.mouse_up(secret, offset=(8, 1))
            await pilot.double_click(secret, offset=(3, 1))
            await pilot.triple_click(secret, offset=(3, 1))
            assert len(copied) == before
            assert not any("secret-value" in text for text in copied)

    asyncio.run(scenario())


def test_editor_selection_copies_only_nonempty_mouse_selection(monkeypatch, tmp_path):
    copied = mock_mac_clipboard(monkeypatch)

    async def scenario():
        app = CopyApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.query_one(Transcript).add_notice("unrelated transcript text")
            editor = app.query_one(ChatEditor)
            editor.load_text("editor words")
            await pilot.pause()
            await pilot.mouse_down(editor, offset=(0, 0))
            await pilot.mouse_up(editor, offset=(8, 0))
            assert copied == ["editor"]
            await pilot.click(editor, offset=(3, 0))
            await pilot.double_click(editor, offset=(3, 0))
            await pilot.triple_click(editor, offset=(3, 0))
            assert copied == ["editor"]

    asyncio.run(scenario())


@pytest.mark.parametrize("gesture", ["drag", "double_click"])
def test_scrollbar_does_not_recopy_old_selection_but_new_selection_copies_again(monkeypatch, tmp_path, gesture):
    copied = mock_mac_clipboard(monkeypatch)

    async def scenario():
        app = CopyApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one(Transcript)
            await transcript.add_notice("\n".join(f"line {i}" for i in range(100)))
            transcript.action_scroll_home()
            await pilot.pause()
            row = transcript.query_one(".notice")

            async def select():
                if gesture == "drag":
                    await pilot.mouse_down(row, offset=(0, 0))
                    await pilot.mouse_up(row, offset=(5, 0))
                else:
                    await pilot.double_click(row, offset=(3, 0))

            await select()
            selected_text = app.screen.get_selected_text()
            assert selected_text and selected_text.startswith("line 0")
            assert copied == [selected_text]
            scrollbar = transcript.vertical_scrollbar
            await pilot.mouse_down(scrollbar, offset=(0, 1))
            await pilot.mouse_up(scrollbar, offset=(0, 4))
            await pilot.pause()
            assert copied == [selected_text]
            await pilot.click(scrollbar, offset=(0, 6))
            assert copied == [selected_text]
            transcript.action_scroll_home()
            await pilot.pause()
            await select()
            assert copied == [selected_text, selected_text]

    asyncio.run(scenario())


def test_triple_click_copies_the_expanded_selection(monkeypatch, tmp_path):
    copied = mock_mac_clipboard(monkeypatch)

    async def scenario():
        app = CopyApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one(Transcript)
            await transcript.add_notice("first line")
            await transcript.add_notice("second line")
            await pilot.pause()
            await pilot.triple_click(transcript.query(".notice").first(), offset=(3, 0))
            assert copied[0] == "first line"
            assert len(copied) == 2
            assert "first line" in copied[-1] and "second line" in copied[-1]
            assert app.clipboard == app.screen.get_selected_text() == copied[-1]

    asyncio.run(scenario())


def test_macos_clipboard_failure_and_ssh_use_textual_fallback(monkeypatch, tmp_path):
    copied = mock_mac_clipboard(monkeypatch)
    app = CopyApp(tmp_path)

    def failed(*_args, **_kwargs):
        raise OSError("clipboard unavailable")

    monkeypatch.setattr(selection_module.subprocess, "run", failed)
    app.copy_to_clipboard("fallback words")
    assert app.clipboard == "fallback words"
    monkeypatch.setenv("SSH_CONNECTION", "remote connection")
    app.copy_to_clipboard("remote words")
    assert app.clipboard == "remote words"
    assert not copied


def test_successful_copy_shows_brief_copied_notice_but_failure_does_not(monkeypatch, make_session):
    copied = mock_mac_clipboard(monkeypatch)

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(notifications=True) as pilot:
            await pilot.pause()
            await app.transcript.add_notice("selected words")
            await pilot.pause()
            await pilot.double_click(app.transcript.query_one(".notice"), offset=(3, 0))
            await pilot.pause()
            notices = list(app.query(Toast))
            assert len(notices) == 1
            assert str(notices[0].render()) == "Copied"
            assert notices[0].region.y == app.query_one("#brand").region.y
            assert notices[0].region.height == 1
            assert notices[0].styles.background.hex == "#7C3AED"
            assert notices[0].styles.color.hex == "#FFFFFF"
            assert copied == ["selected words"]
            assert any("Copied" in strip.text for strip in app.screen._compositor.render_strips())
            await pilot.pause(1.3)
            assert not app.query(Toast)

            def failed(*_args, **_kwargs):
                raise OSError("clipboard unavailable")

            monkeypatch.setattr(selection_module.subprocess, "run", failed)
            app.copy_to_clipboard("not copied to system clipboard")
            app.copy_to_clipboard("")
            await pilot.pause()
            assert not app.query(Toast)

    asyncio.run(scenario())


@pytest.mark.parametrize("sequence", [b"\x1b[13;2u", b"\n"], ids=["shift-enter", "ctrl-j"])
def test_pty_modified_enter_sequence_inserts_newline_without_submitting(project, tmp_path, sequence):
    captured = tmp_path / "captured.jsonl"
    script = """
import json
from pathlib import Path
from virgo.ai.providers import faux
from virgo.cli.main import main

def answer(context):
    with CAPTURE.open('a') as output:
        output.write(json.dumps(context.messages[-1].text()) + '\\n')
    return faux.reply('Received input ' + str(len(faux.CALLS['keys'])))

faux.handler('keys', answer)
raise SystemExit(main(['--ui', 'fullscreen', '--no-extensions', '--no-session', '--model', 'faux/keys']))
""".replace("CAPTURE", f"Path({str(captured)!r})")
    with PtyClient(project, tmp_path / "shift-enter-home", [sys.executable, "-c", script]) as terminal:
        terminal.wait_for(lambda data: b"auto review" in data)
        start = len(terminal.output)
        terminal.send(b"first" + sequence + b"second")
        terminal.wait_for(lambda data: "first" in plain(data) and "second" in plain(data), start=start)
        terminal.pump(0.15)
        assert not captured.exists()
        terminal.send(b"\r")
        terminal.wait_for(lambda data: "Received input 1" in plain(data), start=start)
        assert [json.loads(line) for line in captured.read_text().splitlines()] == ["first\nsecond"]
        terminal.wait_for(lambda data: "Worked for" in plain(data), start=start)
        start = len(terminal.output)
        terminal.send(b"plain enter\r")
        terminal.wait_for(lambda data: "Received input 2" in plain(data), start=start)
        assert [json.loads(line) for line in captured.read_text().splitlines()] == [
            "first\nsecond",
            "plain enter",
        ]
        terminal.send(b"\x04")
        assert terminal.finish() == 0
        terminal.assert_restored()
