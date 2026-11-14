"""Terminal I/O: one stdin reader thread, an event queue, and a streaming renderer.

Lines typed by the user are routed: pending permission question -> answer; otherwise they are
queued for the REPL (which turns them into prompts, or steering messages while the agent works).
"""

from __future__ import annotations

import json
import os
import queue
import select
import shutil
import sys
import threading
import time

from ..agent.types import AgentEvent
from ..ai.web import native_event
from ..diagnostics import sanitize
from ..harness.ui import TIMED_OUT, UI
from .i18n import tr

STYLES = {
    "dim": "\033[2m",
    "error": "\033[31m",
    "warn": "\033[33m",
    "agent": "\033[36m",
    "tool": "\033[35m",
    "ok": "\033[32m",
    "bold": "\033[1m",
}
RESET = "\033[0m"
PROMPT = "› "


def _color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


class ConsoleBroker:
    def __init__(self):
        self.events: queue.Queue = queue.Queue()
        self._ask_lock = threading.Lock()
        self._waiting: threading.Event | None = None
        self._answer = ""
        self._password = False
        self._read_ready = threading.Event()
        self._reading = False
        self._state_lock = threading.Lock()
        self._cancelled = False
        self._input_timeout_cancelled = threading.Event()
        self._prompt_ready: threading.Event | None = None
        self._read_complete: threading.Event | None = None
        self._reader: threading.Thread | None = None
        self._terminal_state = None
        self.closed = False
        self.on_line = lambda: None  # the user's Enter moved the cursor to a fresh line

    def push(self, *item) -> None:
        self.events.put(item)

    def start_reader(self) -> None:
        if sys.stdin.isatty():
            import termios

            self._terminal_state = (sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno()))

        def read():
            while True:
                self._read_ready.wait()
                self._read_ready.clear()
                with self._state_lock:
                    if self.closed:
                        return
                    waiting = self._waiting
                    password = self._password
                    complete = self._read_complete if waiting is not None else None
                    self._reading = True
                try:
                    if waiting is not None and sys.stdin.isatty():
                        line = self._read_terminal_answer(waiting, password)
                    else:
                        if self._prompt_ready is not None:
                            self._prompt_ready.set()
                        line = input()
                except EOFError:
                    self.closed = True
                    if self._waiting:
                        self._waiting.set()
                    self.push("eof")
                    return
                except Exception:  # noqa: BLE001
                    self.cancel_pending()
                    self.resume_reading()
                    continue
                finally:
                    with self._state_lock:
                        self._reading = False
                    if complete is not None:
                        complete.set()
                if line is None or (waiting is not None and waiting.is_set()):
                    continue
                self.on_line()
                if self._waiting is not None:
                    self._answer = line
                    self._waiting.set()
                else:
                    try:
                        import readline

                        if line.strip():
                            readline.add_history(line)
                    except ImportError:
                        pass
                    self.push("line", line)

        self._reader = threading.Thread(target=read, daemon=True, name="virgo-stdin")
        self._reader.start()

    def resume_reading(self) -> None:
        self._read_ready.set()

    def cancel_pending(self) -> None:
        self._cancelled = True
        if self._waiting is not None:
            self._waiting.set()
        if self._prompt_ready is not None:
            self._prompt_ready.set()

    def close(self) -> None:
        self.closed = True
        self.cancel_pending()
        self.resume_reading()
        if self._reader is not None:
            self._reader.join(timeout=0.2)
        if self._terminal_state is not None:
            import termios

            fd, original = self._terminal_state
            termios.tcsetattr(fd, termios.TCSANOW, original)

    def _read_terminal_answer(self, waiting: threading.Event, password: bool) -> str | None:
        """Read forms on the single reader thread, with cancellable hidden input."""
        import termios

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        if password:
            hidden = list(original)
            hidden[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, hidden)
        if self._prompt_ready is not None:
            self._prompt_ready.set()
        value = bytearray()
        try:
            while not waiting.is_set() and not self.closed:
                if not select.select([fd], [], [], 0.05)[0]:
                    continue
                char = os.read(fd, 1)
                if not char:
                    raise EOFError
                if char in (b"\n", b"\r"):
                    return value.decode(sys.stdin.encoding or "utf-8")
                value.extend(char)
            termios.tcflush(fd, termios.TCIFLUSH)
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, original)
            if password:
                sys.stdout.write("\n")
                sys.stdout.flush()

    def read_value(
        self, question: str, write, *, password: bool = False, timeout: float | None = None
    ) -> str | None:
        with self._ask_lock:
            with self._state_lock:
                if self.closed:
                    return None
                if password and self._reading:
                    raise RuntimeError("A password prompt requires the current terminal read to finish")
                ev = threading.Event()
                self._answer = ""
                self._cancelled = False
                self._input_timeout_cancelled.clear()
                self._password = password
                self._waiting = ev
                ready = self._prompt_ready = threading.Event()
                complete = self._read_complete = threading.Event()
                already_reading = self._reading
                if already_reading:
                    ready.set()
            try:
                self.resume_reading()
                ready.wait()
                write(question + "\n> ")
                deadline = None if timeout is None else time.monotonic() + timeout
                while not ev.wait(0.05):
                    if (
                        self._input_timeout_cancelled.is_set()
                        or deadline is None
                        or time.monotonic() < deadline
                    ):
                        continue
                    with self._state_lock:
                        if self._input_timeout_cancelled.is_set():
                            continue
                        answered = ev.is_set()  # the answer may have arrived just now
                        if not answered:
                            self._cancelled = True
                            ev.set()
                    if not answered:
                        write("\n")
                        return TIMED_OUT
                return None if self.closed or self._cancelled else self._answer
            finally:
                # A cancelled form must finish flushing partial input and
                # restoring echo before the next command can be submitted.
                if not already_reading and not self.closed:
                    complete.wait()
                self._waiting = None
                self._answer = ""
                self._password = False
                self._prompt_ready = None
                self._read_complete = None
                if already_reading and not self.closed:
                    # The question borrowed a read that was serving the REPL (e.g. steering
                    # while the agent works); keep reading lines for it afterwards.
                    self.resume_reading()

    def cancel_input_timeouts(self) -> None:
        with self._state_lock:
            self._input_timeout_cancelled.set()

    def ask(self, question: str, write) -> str | None:
        answer = self.read_value(question, write)
        self.resume_reading()
        if answer is None:
            return None
        a = answer.strip().lower()
        return "a" if a in ("a", "always") else "y" if a in ("y", "yes") else "n"


def _brief(args: dict | None) -> str:
    args = args or {}
    for key in ("command", "path", "pattern", "description", "name", "to", "action", "question"):
        if key in args and isinstance(args[key], str):
            v = args[key].replace("\n", " ")
            return v if len(v) < 100 else v[:97] + "..."
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) < 100 else s[:97] + "..."


class TerminalUI(UI):
    interactive = True

    def __init__(self, broker: ConsoleBroker, show_thinking: bool = True):
        self.broker = broker
        self.color = _color()
        self.show_thinking = show_thinking
        self._lock = threading.RLock()
        self._midline = False
        self._block = None  # "text" | "thinking"
        broker.on_line = self.input_done

    def input_done(self) -> None:
        self._midline = False

    # -- output primitives --------------------------------------------------------
    def _style(self, text: str, style: str | None) -> str:
        if not style or not self.color or style not in STYLES:
            return text
        return f"{STYLES[style]}{text}{RESET}"

    def write(self, text: str, style: str | None = None) -> None:
        with self._lock:
            sys.stdout.write(self._style(text, style))
            sys.stdout.flush()
            if text:
                self._midline = not text.endswith("\n")

    def _newline(self) -> None:
        if self._midline:
            self.write("\n")

    def print(self, text: str, style: str | None = None) -> None:
        with self._lock:
            self._newline()
            self.write(text + "\n", style)

    def save_failed(self, session_id: str, path: str, message: str) -> None:
        with self._lock:
            print(message, file=sys.stderr)

    def confirm(self, question: str, agent: str = "main") -> str | None:
        with self._lock:
            self._newline()
        return self.broker.ask(self._style("? ", "warn") + question, self.write)

    def input(
        self, question: str, *, password: bool = False, default: str = "", timeout: float | None = None
    ) -> str | None:
        with self._lock:
            self._newline()
        prompt = question + (f" [{default}]" if default and not password else "")
        value = self.broker.read_value(prompt, self.write, password=password, timeout=timeout)
        if value is TIMED_OUT:
            self.print(tr(self, "ui.console.timeout"), "dim")
            return value
        return default if value == "" else value

    def select(self, question: str, options: list[str], *, timeout: float | None = None) -> str | None:
        if not options:
            return None
        deadline = None if timeout is None else time.monotonic() + timeout
        prompt = "\n".join([question, *(f"  {i}. {label}" for i, label in enumerate(options, 1))])
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            value = self.input(prompt + tr(self, "ui.console.choose"), timeout=left)
            if value is TIMED_OUT:
                return value
            if value is None or not value.strip():
                return None
            deadline = None  # Any typed response establishes that the user is present.
            value = value.strip()
            if value in options:
                return value
            if value.isdecimal() and 1 <= int(value) <= len(options):
                return options[int(value) - 1]
            self.print(tr(self, "ui.console.invalid_choice"), "warn")

    def cancel_input_timeouts(self) -> None:
        self.broker.cancel_input_timeouts()

    def wake(self) -> None:
        self.broker.push("wake")

    def prompt(self) -> None:
        with self._lock:
            self._newline()
            self.write(self._style(PROMPT, "bold"))
        self.broker.resume_reading()

    # -- events -------------------------------------------------------------------
    def on_event(self, ev: AgentEvent) -> None:
        with self._lock:
            if ev.agent != "main":
                self._sub(ev)
                return
            t = ev.type
            if t == "message_update":
                se = ev.stream_event
                if se.type in ("text_start", "thinking_start"):
                    kind = "text" if se.type == "text_start" else "thinking"
                    if self._block != kind:
                        self._newline()
                    self._block = kind
                elif se.type == "text_delta":
                    self.write(se.delta)
                elif se.type == "thinking_delta" and self.show_thinking:
                    self.write(se.delta, "dim")
                elif se.type == "native_tool_end" and 0 <= se.index < len(se.partial.content):
                    block = se.partial.content[se.index]
                    info = native_event(getattr(block, "data", {}))
                    if info:
                        self._block = None
                        self._newline()
                        detail = info.get("result", info["args"])
                        status = info["status"]
                        if status in {"in_progress", "completed", "failed", "incomplete"}:
                            status = tr(self, "ui.native." + status)
                        self.write(f"● {info['name']} · {status} {_brief(detail)}\n", "dim")
                elif se.type == "retry":
                    self._block = None
                    self._newline()
            elif t == "message_end" and getattr(ev.message, "role", "") == "assistant":
                self._block = None
                self._newline()
                m = ev.message
                if m.stop_reason in ("error", "refusal"):
                    self.print(f"✗ {m.error_message}", "error")
                elif m.stop_reason == "aborted":
                    self.print(tr(self, "ui.console.aborted"), "warn")
            elif t == "tool_execution_start":
                self._block = None
                self._newline()
                self.write(f"● {ev.tool_name}", "tool")
                self.write(f"({_brief(ev.args)})\n", "dim")
            elif t == "tool_execution_end":
                text = ev.result.as_text() if ev.result else ""
                lines = text.strip().splitlines() or [tr(self, "ui.no_output")]
                width = shutil.get_terminal_size((100, 20)).columns - 6
                first = lines[0][:width]
                more = tr(self, "ui.console.more_lines", count=len(lines) - 1) if len(lines) > 1 else ""
                self.write(f"  ⎿ {first}{more}\n", "error" if ev.is_error else "dim")

    def _sub(self, ev: AgentEvent) -> None:
        if ev.type == "tool_execution_start":
            self._newline()
            self.write(f"  ↳ [{ev.agent}] {ev.tool_name}({_brief(ev.args)})\n", "dim")
        elif ev.type == "message_end" and getattr(ev.message, "role", "") == "assistant":
            if ev.message.stop_reason in ("error", "refusal"):
                self.print(f"  ↳ [{ev.agent}] ✗ {ev.message.error_message}", "error")


class PrintUI(UI):
    """`-p` mode: nothing interactive; messages go to stderr, the answer to stdout."""

    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode
        self.command_output = False
        self._lock = threading.Lock()  # sub-agents and teammates emit from other threads

    def command_result(self, text: str) -> None:
        with self._lock:
            self.command_output = True
            print(
                json.dumps({"type": "command_result", "text": text}, ensure_ascii=False)
                if self.json_mode
                else text
            )

    def print(self, text: str, style: str | None = None) -> None:
        if style in ("error", "warn"):
            with self._lock:
                print(text, file=sys.stderr)

    def save_failed(self, session_id: str, path: str, message: str) -> None:
        self.print(message, "error")
        if self.json_mode:
            with self._lock:
                print(
                    json.dumps(
                        {
                            "type": "session_save_failed",
                            "session_id": session_id,
                            "path": path,
                            "message": message,
                        }
                    )
                )

    def on_event(self, ev: AgentEvent) -> None:
        if self.json_mode:
            line = json.dumps(event_json(ev), ensure_ascii=False, default=str)
            with self._lock:  # one complete JSONL record per write
                sys.stdout.write(line + "\n")
                sys.stdout.flush()


def event_json(ev: AgentEvent) -> dict:
    from dataclasses import asdict

    d: dict = {"type": ev.type, "agent": ev.agent}
    if ev.type == "message_update" and ev.stream_event is not None:
        d["event"] = ev.stream_event.type
        if ev.stream_event.delta:
            d["delta"] = ev.stream_event.delta
        se = ev.stream_event
        if se.type == "retry" and se.partial.error_details is not None:
            d["error_details"] = sanitize(se.partial.error_details)
        if se.type.startswith("native_tool_") and 0 <= se.index < len(se.partial.content):
            info = native_event(getattr(se.partial.content[se.index], "data", {}))
            if info:
                d["native"] = info
    elif ev.type == "message_end" and ev.message is not None:
        d["message"] = asdict(ev.message)
    elif ev.type.startswith("tool_execution"):
        d.update(tool_call_id=ev.tool_call_id, tool_name=ev.tool_name, args=ev.args)
        if ev.result is not None:
            d.update(result=ev.result.as_text(), is_error=ev.is_error)
            if isinstance(ev.result.details, dict):
                if ev.result.details.get("error") is not None:
                    d["error_details"] = sanitize(ev.result.details["error"])
                if ev.result.details.get("related_errors"):
                    d["related_errors"] = sanitize(ev.result.details["related_errors"])
    return d
