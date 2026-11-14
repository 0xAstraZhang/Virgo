"""Plain-console output and login forms, driven through an actual terminal without credentials."""

from __future__ import annotations

import json
import signal
import sys
import threading

import pytest

from virgo.agent.types import ToolResult
from virgo.ai.types import TextContent

from tests.support.terminal import PtyClient, plain, termios

LOGIN_SCRIPT = """
from virgo.cli.console import ConsoleBroker, TerminalUI
from virgo.cli.repl import run_repl
from virgo.harness.session import AgentSession

broker = ConsoleBroker()
ui = TerminalUI(broker)
session = AgentSession(ui=ui, model='faux/demo', session=None, extensions=False)

def login(session, args):
    method = ui.select('Connection method', ['Auth', 'API'])
    if method is None:
        ui.print('Login cancelled.')
        return
    secret = ui.input('Secret key', password=True)
    if secret is None:
        ui.print('Login cancelled.')
        return
    model = ui.input('Default model', default='demo-model')
    if model is None:
        ui.print('Login cancelled.')
        return
    ui.print(f'Configured {method} / {model}; key length={len(secret)}')

session.commands._cmds['login'].fn = login  # Replace the test fixture callback, not registration.
run_repl(session, ui, broker)
"""


def assert_terminal_restored(restored: list, original: list) -> None:
    # macOS marks buffered input for redisplay after SIGINT. PENDIN is kernel
    # bookkeeping that may remain after restoring the echo and canonical modes.
    for attributes in (restored, original):
        attributes[3] &= ~getattr(termios, "PENDIN", 0)
    assert restored == original


def begin_login(terminal: PtyClient) -> None:
    start = len(terminal.output)
    terminal.send(b"/login\r")
    terminal.wait_for(lambda data: "Connection method" in plain(data), start=start)
    assert termios.tcgetattr(terminal.slave)[3] & termios.ECHO
    terminal.send(b"2\r")
    terminal.wait_for(lambda data: "Secret key" in plain(data), start=start)
    assert not termios.tcgetattr(terminal.slave)[3] & termios.ECHO


def finish_login(terminal: PtyClient, secret: bytes) -> None:
    start = len(terminal.output)
    terminal.send(secret + b"\r")
    terminal.wait_for(lambda data: "Default model [demo-model]" in plain(data), start=start)
    assert termios.tcgetattr(terminal.slave)[3] & termios.ECHO
    terminal.send(b"\r")
    terminal.wait_for(
        lambda data: f"Configured API / demo-model; key length={len(secret)}" in plain(data), start=start
    )


@pytest.mark.pty
def test_plain_forms_hide_password_keep_defaults_and_exclude_answers_from_history(project, tmp_path):
    home = tmp_path / "plain-auth-home"
    secret = b"sk-private-key-123"
    with PtyClient(project, home, [sys.executable, "-c", LOGIN_SCRIPT]) as terminal:
        terminal.wait_for(lambda data: " /help for commands" in plain(data))
        begin_login(terminal)
        finish_login(terminal, secret)

        start = len(terminal.output)
        terminal.send(b"normal chat after login\r")
        terminal.wait_for(lambda data: "(faux) You said: normal chat after login" in plain(data), start=start)
        terminal.send(b"/exit\r")
        assert terminal.finish() == 0
        restored, original = termios.tcgetattr(terminal.slave), terminal.before
        assert secret not in terminal.output

    history = (home / "history").read_text().replace(r"\040", " ")  # macOS libedit escapes spaces.
    assert "normal chat after login" in history
    assert "/login" in history
    assert secret.decode() not in history
    assert "demo-model" not in history
    assert "2" not in history.splitlines()
    assert_terminal_restored(restored, original)


@pytest.mark.pty
def test_plain_password_cancel_restores_terminal_and_allows_another_login(project, tmp_path):
    home = tmp_path / "plain-cancel-home"
    abandoned = b"unfinished-private-key"
    secret = b"replacement-private-key"
    with PtyClient(project, home, [sys.executable, "-c", LOGIN_SCRIPT]) as terminal:
        terminal.wait_for(lambda data: " /help for commands" in plain(data))
        begin_login(terminal)
        terminal.send(abandoned)
        terminal.pump(0.1)
        start = len(terminal.output)
        terminal.process.send_signal(signal.SIGINT)
        terminal.wait_for(lambda data: "Login cancelled." in plain(data), start=start)

        begin_login(terminal)
        finish_login(terminal, secret)
        terminal.send(b"/exit\r")
        assert terminal.finish() == 0
        restored, original = termios.tcgetattr(terminal.slave), terminal.before
        assert abandoned not in terminal.output
        assert secret not in terminal.output

    history = (home / "history").read_text()
    assert abandoned.decode() not in history
    assert secret.decode() not in history
    assert_terminal_restored(restored, original)


def test_print_mode_json_lines_stay_whole_across_threads(monkeypatch):
    from virgo.agent.types import AgentEvent
    from virgo.cli.console import PrintUI

    class SlowStdout:
        """Writes each record in two halves and, between them, waits for a second writer to arrive.

        Without serialisation the second thread's half lands inside the first record every time;
        with it the second writer cannot enter, so the wait times out and the record stays whole.
        """

        def __init__(self):
            self.parts: list[str] = []
            self.arrived = threading.Semaphore(0)

        def write(self, text: str) -> int:
            half = len(text) // 2
            self.parts.append(text[:half])
            self.arrived.release()  # signal only once our first half is down
            if self.arrived.acquire(timeout=0.3):  # our own release, consumed once
                self.arrived.acquire(timeout=0.3)  # the other writer's arrival, if it can get in
            self.parts.append(text[half:])
            return len(text)

        def flush(self) -> None:
            pass

    out = SlowStdout()
    monkeypatch.setattr(sys, "stdout", out)
    ui = PrintUI(json_mode=True)

    def emit(tag: str):
        result = ToolResult([TextContent(tag * 4000)])
        ui.on_event(AgentEvent("tool_execution_end", "worker", tool_call_id="c", tool_name="t", result=result))

    threads = [threading.Thread(target=emit, args=(tag,)) for tag in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = "".join(out.parts).splitlines()
    assert sorted(json.loads(line)["result"] for line in lines) == ["a" * 4000, "b" * 4000]


def test_plain_question_timeout_can_be_cancelled_without_answering(monkeypatch):
    from virgo.cli.console import ConsoleBroker, TerminalUI
    from virgo.harness.ui import TIMED_OUT

    broker = ConsoleBroker()
    # The terminal reader is already active; provide its eventual answer explicitly.
    broker._reading = True
    monkeypatch.setattr(broker, "resume_reading", lambda: None)
    ui = TerminalUI(broker)
    shown = threading.Event()
    monkeypatch.setattr(ui, "write", lambda *args: shown.set())
    results = []
    worker = threading.Thread(target=lambda: results.append(ui.input("Where?", timeout=0.2)))
    worker.start()
    try:
        assert shown.wait(3)
        ui.cancel_input_timeouts()
        worker.join(0.4)
        assert worker.is_alive() and results == []
        with broker._state_lock:
            broker._answer = "staging"
            broker._waiting.set()
        worker.join(3)
        assert not worker.is_alive() and results == ["staging"]
        # Cancelling one form's deadline does not disable timeouts on future forms.
        assert ui.input("Next?", timeout=0.01) is TIMED_OUT
    finally:
        broker.close()
        worker.join(3)
