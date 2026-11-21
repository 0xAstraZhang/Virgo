"""Exercise the plain reader in a private native Windows console, including on headless CI."""

from __future__ import annotations

import subprocess
import sys

import pytest


def _exercise_console():
    import msvcrt

    import win32con
    import win32console

    from virgo.cli.console import ConsoleBroker
    from virgo.harness.ui import TIMED_OUT

    with open("CONIN$", encoding="utf-8") as stdin, open("CONOUT$", "w", encoding="utf-8") as stdout:
        sys.stdin, sys.stdout = stdin, stdout
        keys = win32console.PyConsoleScreenBufferType(msvcrt.get_osfhandle(stdin.fileno()))
        screen = win32console.PyConsoleScreenBufferType(msvcrt.get_osfhandle(stdout.fileno()))

        def type_text(text, *, alt_numpad=False):
            records = []
            encoded = text.encode("utf-16-le")
            for i in range(0, len(encoded), 2):
                event = win32console.PyINPUT_RECORDType(win32console.KEY_EVENT)
                event.Char = chr(int.from_bytes(encoded[i : i + 2], "little"))
                event.KeyDown = not alt_numpad
                if alt_numpad:
                    event.VirtualKeyCode = win32con.VK_MENU
                event.RepeatCount = 1
                records.append(event)
            keys.WriteConsoleInput(records)

        broker = ConsoleBroker()

        def abandon(_prompt):
            type_text("abandoned-secret")
            broker.cancel_pending()

        def enter_name(_prompt):
            type_text("à", alt_numpad=True)
            type_text("可見\U0001f600\b字\r")

        broker.start_reader()
        try:
            assert broker.read_value("Secret", abandon, password=True) is None
            assert broker.read_value("Timeout", lambda _: None, timeout=0.05) is TIMED_OUT
            assert broker.read_value("Name", enter_name) == "à可見字"
            secret = broker.read_value("Secret", lambda _: type_text("private-key\r"), password=True)
            assert secret == "private-key"
            broker.resume_reading()
            type_text("next line\r")
            assert broker.events.get(timeout=5) == ("line", "next line")
            size = screen.GetConsoleScreenBufferInfo()["Size"]
            output = screen.ReadConsoleOutputCharacter(size.X * 8, win32console.PyCOORDType(0, 0))
            assert "可見字" in output, ascii(output.rstrip())
            assert "private-key" not in output and "abandoned-secret" not in output
        finally:
            broker.close()
            sys.stdin, sys.stdout = sys.__stdin__, sys.__stdout__


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows console")
def test_windows_console_forms_and_next_command():
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        [sys.executable, __file__],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startup,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


if __name__ == "__main__":
    _exercise_console()
