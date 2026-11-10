"""Drive the installed fullscreen CLI through a real, isolated pseudo terminal.

Unlike Pilot tests these exercise the terminal driver, paste decoder, resize
signals, and terminal restoration. No user Terminal window is opened or changed.
"""

from __future__ import annotations

import errno
import os
import re
import select
import signal
import struct
import subprocess
import sys
import time

import pytest

fcntl = pytest.importorskip("fcntl")
pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")

ANSI = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]")
ENTER_SCREEN = b"\x1b[?1049h"
LEAVE_SCREEN = b"\x1b[?1049l"


def plain(data: bytes) -> str:
    return ANSI.sub(b"", data).decode("utf-8", errors="replace")


class PtyClient:
    def __init__(self, project, home, argv=None):
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)
        self.output = bytearray()
        self.resize(80, 24, notify=False)
        env = {key: value for key, value in os.environ.items() if not key.startswith("TEXTUAL_")}
        env.update(
            TERM="xterm-256color",
            COLORTERM="truecolor",
            VIRGO_HOME=str(home),
            PYTHONUNBUFFERED="1",
            TEXTUAL_ANIMATIONS="none",
        )
        env.pop("VIRGO_MODEL", None)
        self.process = subprocess.Popen(
            argv
            or [
                sys.executable,
                "-m",
                "virgo",
                "--ui",
                "fullscreen",
                "--no-extensions",
                "--no-session",
                "--model",
                "faux/demo",
            ],
            cwd=project,
            env=env,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)
        os.close(self.slave)

    def resize(self, width: int, height: int, *, notify: bool = True) -> None:
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        if notify:
            self.process.send_signal(signal.SIGWINCH)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def pump(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            readable, _, _ = select.select(
                [self.master], [], [], max(0, min(0.05, deadline - time.monotonic()))
            )
            if readable:
                try:
                    data = os.read(self.master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        return
                    raise
                if not data:
                    return
                self.output.extend(data)

    def wait_for(self, predicate, *, start: int = 0, timeout: float = 10) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = bytes(self.output[start:])
            if predicate(data):
                return data
            self.pump(0.05)
        data = bytes(self.output[start:])
        pytest.fail(f"PTY expectation timed out (exit={self.process.poll()}):\n{plain(data)[-2500:]}")

    def finish(self, timeout: float = 10) -> int:
        self.wait_for(lambda _: self.process.poll() is not None, timeout=timeout)
        self.pump(0.1)
        return self.process.returncode

    def assert_restored(self) -> None:
        assert LEAVE_SCREEN in self.output
        assert b"\x1b[?2004l" in self.output  # Bracketed paste disabled.
        assert b"\x1b[?25h" in self.output  # Cursor visible again.
        assert termios.tcgetattr(self.slave) == self.before


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


def test_fullscreen_pty_session_browser_and_empty_session_cleanup(project, tmp_path):
    home = tmp_path / "pty-session-home"
    argv = [sys.executable, "-m", "virgo", "--ui", "fullscreen", "--no-extensions", "--model", "faux/demo"]
    with PtyClient(project, home, argv) as terminal:
        terminal.wait_for(lambda data: ENTER_SCREEN in data and b"auto review" in data)
        before_browser = len(terminal.output)
        terminal.send(b"/sessions\r")
        terminal.wait_for(
            lambda data: "Group: Workspace" in plain(data) and "Untitled session" in plain(data),
            start=before_browser,
        )
        before_group = len(terminal.output)
        terminal.send(b"g")
        terminal.wait_for(lambda data: "Group: None" in plain(data), start=before_group)

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
