"""Exercise plain login forms through an actual terminal without real credentials."""

from __future__ import annotations

import signal
import sys

from tests.test_tui_pty import PtyClient, plain, termios

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

session.commands.register('login', login, 'test login form')
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
