"""Mouse selection reaches the real clipboard path; Enter stays distinct from newline."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

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
from virgo.cli.tui.transcript import AssistantView, ToolView, Transcript

from tests.support.terminal import PtyClient, plain


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

    # Replace only the clipboard module adapter; shell discovery also uses subprocess.run.
    monkeypatch.setattr(selection_module, "subprocess", SimpleNamespace(
        run=run, DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError,
    ))
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
            result = next(t for t in transcript.query(ToolView) if t.kind == "tool_result" and t.call_id == "tool")
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


@pytest.fixture
def clipboard_app(tmp_path, monkeypatch):
    app = CopyApp(tmp_path)
    app._driver = Mock()
    monkeypatch.setattr(app, "notify", Mock())
    for name in ("SSH_CONNECTION", "SSH_TTY", "WSL_DISTRO_NAME", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    return app


def assert_terminal_copy(app, text, *, toast):
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    app._driver.write.assert_called_once_with(f"\x1b]52;c;{encoded}\a")
    assert app.clipboard == text
    if toast:
        app.notify.assert_called_once_with("Copied", timeout=1.2, markup=False)
    else:
        app.notify.assert_not_called()


@pytest.mark.parametrize("outcome", ["success", "locked", "write_error", "import_error"])
def test_windows_unicode_clipboard_retry_cleanup_and_fallback(monkeypatch, clipboard_app, outcome):
    class ClipboardError(Exception):
        pass

    clipboard = Mock(error=ClipboardError, CF_UNICODETEXT=13)
    monkeypatch.setitem(sys.modules, "win32clipboard", None if outcome == "import_error" else clipboard)
    monkeypatch.setattr(selection_module, "sys", SimpleNamespace(platform="win32"))
    sleep = Mock()
    monkeypatch.setattr(selection_module, "time", SimpleNamespace(sleep=sleep))
    clipboard.OpenClipboard.side_effect = {
        "success": [ClipboardError(), None], "locked": ClipboardError(), "write_error": None,
        "import_error": None,
    }[outcome]
    if outcome == "write_error":
        clipboard.SetClipboardData.side_effect = OSError("write failed")

    text = "复制到剪贴板"
    clipboard_app.copy_to_clipboard(text)
    if outcome == "success":
        assert clipboard.mock_calls == [
            call.OpenClipboard(), call.OpenClipboard(), call.EmptyClipboard(),
            call.SetClipboardData(13, text), call.CloseClipboard(),
        ]
        sleep.assert_called_once_with(0.015)
        assert clipboard_app.clipboard == text
        clipboard_app.notify.assert_called_once_with("Copied", timeout=1.2, markup=False)
        clipboard_app._driver.write.assert_not_called()
    else:
        assert_terminal_copy(clipboard_app, text, toast=False)
        if outcome == "locked":
            assert clipboard.mock_calls == [call.OpenClipboard()] * 3
            assert sleep.call_args_list == [call(0.015)] * 2
        elif outcome == "write_error":
            clipboard.CloseClipboard.assert_called_once_with()
            sleep.assert_not_called()


@pytest.mark.parametrize("platform,ssh", [("win32", "SSH_CONNECTION"), ("linux", "SSH_TTY")])
def test_ssh_uses_terminal_clipboard_without_touching_local_clipboard(monkeypatch, clipboard_app, platform, ssh):
    monkeypatch.setattr(selection_module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv(ssh, "remote")
    monkeypatch.setattr(selection_module, "_copy_windows", lambda _: pytest.fail("local clipboard over SSH"))
    monkeypatch.setattr(selection_module, "_clipboard_command", lambda: pytest.fail("local clipboard over SSH"))
    clipboard_app.copy_to_clipboard("remote selection")
    assert_terminal_copy(clipboard_app, "remote selection", toast=True)


@pytest.mark.parametrize("wsl,tools,fallback,command,encoding", [
    ("env", ["clip.exe", "wl-copy"], False, ["/bin/clip.exe"], "utf-16-le"),
    ("proc", [], True, ["/mnt/c/Windows/System32/clip.exe"], "utf-16-le"),
    ("", ["wl-copy", "xclip"], False, ["/bin/wl-copy"], "utf-8"),
    ("", ["xclip", "xsel"], False, ["/bin/xclip", "-selection", "clipboard"], "utf-8"),
    ("", ["xsel"], False, ["/bin/xsel", "--clipboard", "--input"], "utf-8"),
])
def test_linux_clipboard_command_and_encoding(monkeypatch, tmp_path, clipboard_app, wsl, tools, fallback, command, encoding):
    monkeypatch.setattr(selection_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    if wsl == "env":
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    version = tmp_path / "version"
    version.write_text("Linux MiCroSoft WSL2" if wsl == "proc" else "Linux generic")
    clip = tmp_path / "clip.exe"
    if fallback:
        clip.touch()
    paths = {"/proc/version": version, "/mnt/c/Windows/System32/clip.exe": clip}
    monkeypatch.setattr(selection_module, "Path", paths.__getitem__)
    monkeypatch.setattr(selection_module.shutil, "which", lambda name: "/bin/" + name if name in tools else None)
    run = Mock()
    monkeypatch.setattr(selection_module.subprocess, "run", run)
    text = "第一行\n第二行"
    clipboard_app.copy_to_clipboard(text)
    run.assert_called_once_with(command, input=text.encode(encoding), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=1, check=True)
    assert clipboard_app.clipboard == text
    clipboard_app.notify.assert_called_once_with("Copied", timeout=1.2, markup=False)
    clipboard_app._driver.write.assert_not_called()


@pytest.mark.parametrize("wsl,available", [(False, True), (False, False), (True, True), (True, False)])
def test_linux_clipboard_terminal_fallback_toast_only_when_no_native_tool(monkeypatch, tmp_path, clipboard_app, wsl, available):
    monkeypatch.setattr(selection_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(selection_module, "_is_wsl", lambda: wsl)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(selection_module.shutil, "which", lambda name: name if available else None)
    monkeypatch.setattr(selection_module, "Path", lambda _: tmp_path / "absent")
    run = Mock(side_effect=subprocess.TimeoutExpired("clipboard", 1))
    monkeypatch.setattr(selection_module.subprocess, "run", run)
    clipboard_app.copy_to_clipboard("fallback")
    assert run.call_count == int(available)
    assert_terminal_copy(clipboard_app, "fallback", toast=not wsl and not available)


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


@pytest.mark.pty
@pytest.mark.parametrize("sequence", [b"\x1b[13;2u", b"\n", b"\x1b[13;3u", b"\\\r"],
                         ids=["shift-enter", "ctrl-j", "alt-enter", "backslash-enter"])
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
raise SystemExit(main(['--ui', 'fullscreen', '--no-extensions', '--no-session', '--permission', 'no-limit', '--model', 'faux/keys']))
""".replace("CAPTURE", f"Path({str(captured)!r})")
    with PtyClient(project, tmp_path / "shift-enter-home", [sys.executable, "-c", script]) as terminal:
        terminal.wait_for(lambda data: b"Host" in data)
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
