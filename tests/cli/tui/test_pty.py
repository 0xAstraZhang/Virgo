"""Drive the installed fullscreen CLI through a real, isolated pseudo terminal.

Unlike Pilot tests these exercise the terminal driver, paste decoder, resize
signals, and terminal restoration. No user Terminal window is opened or changed.
"""

from __future__ import annotations

import json
import re
import struct
import sys

import pytest

from tests.support.terminal import ENTER_SCREEN, PtyClient, fcntl, plain, termios

pytestmark = pytest.mark.pty


@pytest.mark.parametrize("draft", ["", "unfinished draft "])
def test_fullscreen_pty_history_round_trip_past_slash_command(project, tmp_path, draft):
    home = tmp_path / "pty-history-home"
    home.mkdir()
    entries = ["older input", "/login", "newer input"]
    (home / "tui-history.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    with PtyClient(project, home) as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data and b"auto review" in data)
        if draft:
            terminal.send(draft.encode())
            terminal.wait_for(lambda data: draft in plain(data))
        # Both directions must cross a slash command without its menu stealing arrows.
        for key, expected in [
            (b"\x1b[A", "newer input"),
            (b"\x1b[A", "/login"),
            (b"\x1b[A", "older input"),
            (b"\x1b[B", "/login"),
            (b"\x1b[B", "newer input"),
        ]:
            start = len(terminal.output)
            terminal.send(key)
            terminal.wait_for(lambda data, expected=expected: expected in plain(data), start=start)
        terminal.send(b"\x1b[B")
        terminal.pump(0.2)
        # Submitting a suffix proves we restored exactly the draft (including blank).
        start = len(terminal.output)
        terminal.send(b"history-sentinel\r")
        terminal.wait_for(
            lambda data: f"(faux) You said: {draft}history-sentinel" in plain(data), start=start
        )
        terminal.send(b"\x04")
        assert terminal.finish() == 0
        terminal.assert_restored()


def test_fullscreen_pty_paste_resize_and_clean_exit(project, tmp_path):
    with PtyClient(project, tmp_path / "pty-home") as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data and b"auto review" in data)
        assert b"\x1b[?2004h" in terminal.output
        assert b"V_" in terminal.output
        assert not termios.tcgetattr(terminal.slave)[3] & termios.ICANON
        assert not termios.tcgetattr(terminal.slave)[3] & termios.ECHO
        assert re.search(rb"\x1b\[24;\d+H", terminal.output)

        # Terminal.app emits CSI Z for Shift+Tab; cycle all three permissions.
        for label in ("accept all", "plan", "auto review"):
            before_switch = len(terminal.output)
            terminal.send(b"\x1b[Z")
            terminal.wait_for(lambda data, label=label: label in plain(data), start=before_switch)

        # A single bracketed paste contains a newline but must remain in the editor.
        before_paste = len(terminal.output)
        terminal.send(b"\x1b[200~" + "你好，Virgo\nsecond line".encode() + b"\x1b[201~")
        terminal.wait_for(
            lambda data: "你好，Virgo" in plain(data) and "second line" in plain(data), start=before_paste
        )
        terminal.pump(0.2)
        assert "(faux)" not in plain(bytes(terminal.output))

        before_submit = len(terminal.output)
        terminal.send(b"\r")
        response = terminal.wait_for(lambda data: "(faux) You said:" in plain(data), start=before_submit)
        assert "你好，Virgo" in plain(response)
        assert "second line" in plain(response)

        # Resize the actual PTY and deliver the OS signal, rather than changing a test-only size.
        before_resize = len(terminal.output)
        terminal.resize(120, 40)
        terminal.wait_for(lambda data: bool(re.search(rb"\x1b\[40;\d+H", data)), start=before_resize)
        assert struct.unpack("HHHH", fcntl.ioctl(terminal.slave, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (
            40,
            120,
        )

        terminal.send(b"\x04")  # Ctrl+D with the now-empty editor.
        assert terminal.finish() == 0
        terminal.assert_restored()
        assert not list((tmp_path / "pty-home").glob("sessions/**/*.jsonl"))


def test_fullscreen_pty_exception_restores_terminal(project, tmp_path):
    # Raise from an actual Textual callback after the terminal driver has entered
    # fullscreen mode. Keep the real CLI, App.run(), and run_fullscreen finally.
    script = """
from virgo.cli.main import main
from virgo.cli.tui.app import VirgoApp

original_mount = VirgoApp.on_mount

def fail():
    raise RuntimeError('intentional-pty-render-failure')

def mount(self):
    original_mount(self)
    self.set_timer(0.15, fail)

VirgoApp.on_mount = mount
raise SystemExit(main(['--ui', 'fullscreen', '--no-extensions', '--no-session', '--model', 'faux/demo']))
"""
    with PtyClient(project, tmp_path / "pty-error-home", [sys.executable, "-c", script]) as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data)
        assert terminal.finish() != 0
        assert "intentional-pty-render-failure" in plain(bytes(terminal.output))
        terminal.assert_restored()


def test_fullscreen_pty_animates_while_waiting_and_keeps_elapsed_summary(project, tmp_path):
    script = """
from virgo.ai.providers import faux
from virgo.cli.main import main

faux.script('activity', faux.reply('Finished waiting.', delay=1.6))
raise SystemExit(main(['--ui', 'fullscreen', '--no-extensions', '--no-session',
                      '--model', 'faux/activity', '--effort', 'high', 'hello']))
"""
    with PtyClient(project, tmp_path / "pty-activity-home", [sys.executable, "-c", script]) as terminal:
        terminal.wait_for(lambda data: "Thinking…" in plain(data))
        output = terminal.wait_for(lambda data: "Worked for" in plain(data))
        text = plain(output)
        assert "thinking with high effort" in text
        assert "(1s" in text
        assert len(set(text) & set("✢✳✶✻✽")) >= 2
        assert "Finished waiting." in text
        assert re.search(r"Worked for \d+s · done \d\d:\d\d", text)
        terminal.send(b"\x04")
        assert terminal.finish() == 0
        terminal.assert_restored()


def test_fullscreen_pty_session_browser_and_empty_session_cleanup(project, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    home = tmp_path / "pty-session-home"
    argv = [sys.executable, "-m", "virgo", "--ui", "fullscreen", "--no-extensions", "--model", "faux/demo"]
    with PtyClient(project, home, argv) as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data and b"auto review" in data)
        before_browser = len(terminal.output)
        terminal.send(b"/sessions\r")
        browser = terminal.wait_for(
            lambda data: "G Ungroup" in plain(data) and "Untitled session" in plain(data),
            start=before_browser,
        )
        text = plain(browser)
        assert "Sessions" not in text and "Group:" not in text and "All 1" not in text
        # The initial input submission can repaint the editor border before the modal.
        assert "─" not in text[text.index("Session ") :]
        assert "Esc Back" in text and "↑/↓ Select" in text and "Enter Open" in text
        assert "N New session" in text and "Ctrl+X Move to trash" in text
        assert b"38;2;37;99;235" in browser  # Workspace path is blue (#2563EB).
        before_group = len(terminal.output)
        terminal.send(b"g")
        terminal.wait_for(lambda data: "G Group by workspace" in plain(data), start=before_group)

        before_new = len(terminal.output)
        terminal.send(b"n")
        terminal.wait_for(lambda data: "auto review" in plain(data), start=before_new)
        before_model = len(terminal.output)
        terminal.send(b"/model faux/demo\r")
        terminal.wait_for(lambda data: "model: faux/demo" in plain(data), start=before_model)
        terminal.wait_for(lambda _: bool(list(home.glob("sessions/**/*.jsonl"))))
        paths = list(home.glob("sessions/**/*.jsonl"))
        assert any('"type": "model_change"' in path.read_text() for path in paths)

        terminal.send(b"/exit\r")
        assert terminal.finish() == 0
        terminal.assert_restored()
        assert not list(home.glob("sessions/**/*.jsonl"))

    with PtyClient(project, home, argv) as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data and b"auto review" in data)
        terminal.send(b"/sessions\r")
        terminal.wait_for(lambda data: "G Group by workspace" in plain(data))
        terminal.send(b"\x04")
        assert terminal.finish() == 0
        terminal.assert_restored()


def test_fullscreen_pty_new_session_while_first_reply_is_running(project, tmp_path):
    home = tmp_path / "pty-concurrent-home"
    script = """
from virgo.ai.providers import faux
from virgo.cli.main import main
faux.script('concurrent', faux.reply('old-terminal-answer', delay=30), faux.reply('new-terminal-answer'))
raise SystemExit(main(['--ui', 'fullscreen', '--no-extensions', '--model', 'faux/concurrent', 'old-terminal-question']))
"""
    with PtyClient(project, home, [sys.executable, "-c", script]) as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data and "Thinking" in plain(data))
        before_browser = len(terminal.output)
        terminal.send(b"\x12")
        terminal.wait_for(lambda data: "Session" in plain(data) and "Updated" in plain(data) and "Running" in plain(data), start=before_browser)
        before_new = len(terminal.output)
        terminal.send(b"n")
        terminal.wait_for(lambda data: "auto review" in plain(data), start=before_new)
        before_reply = len(terminal.output)
        terminal.send(b"new-terminal-question\r")
        terminal.wait_for(lambda data: "new-terminal-answer" in plain(data), start=before_reply)
        assert "old-terminal-answer" not in plain(bytes(terminal.output))
        before_browser = len(terminal.output)
        terminal.send(b"\x12")
        terminal.wait_for(
            lambda data: "old-terminal-question" in plain(data) and "Running" in plain(data), start=before_browser,
        )
        terminal.send(b"\x04")
        assert terminal.finish() == 0
        terminal.assert_restored()
        files = list(home.glob("sessions/**/*.jsonl"))
        assert len(files) == 2
        texts = [path.read_text(encoding="utf-8") for path in files]
        old = next(text for text in texts if 'old-terminal-question' in text)
        new = next(text for text in texts if 'new-terminal-question' in text)
        assert 'new-terminal-answer' in new and 'old-terminal-question' not in new
        assert 'new-terminal-question' not in old and 'new-terminal-answer' not in old
