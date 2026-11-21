"""Cancellable plain-console forms using native Windows keyboard events."""

from __future__ import annotations

import codecs
import msvcrt
import sys
import threading

import win32con
import win32console
import win32event


def read_answer(waiting: threading.Event, ready: threading.Event, *, password: bool) -> str | None:
    console = win32console.PyConsoleScreenBufferType(msvcrt.get_osfhandle(sys.stdin.fileno()))
    # ReadConsoleInput bypasses line editing and echo. Only echo visible form fields.
    decoder = codecs.getincrementaldecoder("utf-16-le")(errors="replace")
    value: list[str] = []
    ready.set()
    try:
        while not waiting.is_set():
            if win32event.WaitForSingleObject(console, 50) == win32event.WAIT_TIMEOUT:
                continue
            (event,) = console.ReadConsoleInput(1)
            if event.EventType != win32console.KEY_EVENT:
                continue
            # Alt+numpad delivers the composed character when the Alt key is released.
            if not event.KeyDown and event.VirtualKeyCode != win32con.VK_MENU:
                continue
            char = event.Char
            if char in ("\r", "\n"):
                return "".join(value)
            if char == "\x1a" and not value:  # Ctrl-Z at the start of a line
                raise EOFError
            if char == "\b":
                for _ in range(min(event.RepeatCount, len(value))):
                    value.pop()
                    if not password:
                        sys.stdout.write("\b \b")
            elif char >= " " or char == "\t":
                # A KEY_EVENT carries one UTF-16 code unit, including for pasted text.
                chars = decoder.decode(char.encode("utf-16-le", "surrogatepass") * event.RepeatCount)
                value.extend(chars)
                if not password:
                    sys.stdout.write(chars)
            if not password:
                sys.stdout.flush()
        return None
    finally:
        if waiting.is_set():
            console.FlushConsoleInputBuffer()
        print(flush=True)
