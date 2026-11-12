"""Drive a real process through an isolated pseudo terminal (POSIX only).

Unlike Pilot tests these exercise the terminal driver, paste decoder, resize signals, and terminal
restoration. No user Terminal window is opened or changed. Tests that use ``PtyClient`` carry the
``pty`` marker; the root conftest skips them where pseudo terminals are unavailable.
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

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows: importing this module stays safe; the marked tests are skipped
    fcntl = pty = termios = None

HAS_PTY = pty is not None

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
