"""Terminal I/O: one stdin reader thread, an event queue, and a streaming renderer.

Lines typed by the user are routed: pending permission question -> answer; otherwise they are
queued for the REPL (which turns them into prompts, or steering messages while the agent works).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading

from ..agent.types import AgentEvent
from ..harness.ui import UI

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
        self.closed = False
        self.on_line = lambda: None  # the user's Enter moved the cursor to a fresh line

    def push(self, *item) -> None:
        self.events.put(item)

    def start_reader(self) -> None:
        def read():
            while True:
                try:
                    line = input()
                except EOFError:
                    self.closed = True
                    if self._waiting:
                        self._waiting.set()
                    self.push("eof")
                    return
                except Exception:  # noqa: BLE001
                    continue
                self.on_line()
                if self._waiting is not None:
                    self._answer = line
                    self._waiting.set()
                else:
                    self.push("line", line)

        threading.Thread(target=read, daemon=True, name="virgo-stdin").start()

    def ask(self, question: str, write) -> str | None:
        with self._ask_lock:  # one question at a time, from any thread
            if self.closed:
                return None
            ev = threading.Event()
            self._answer = ""
            self._waiting = ev
            write(question + "\n> ")
            ev.wait()
            self._waiting = None
            if self.closed:
                return None
            a = self._answer.strip().lower()
            return "a" if a in ("a", "always") else "y" if a in ("y", "yes") else "n"


def _brief(name: str, args: dict | None) -> str:
    args = args or {}
    for key in ("command", "path", "pattern", "description", "name", "to", "action"):
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

    def confirm(self, question: str, agent: str = "main") -> str | None:
        with self._lock:
            self._newline()
        return self.broker.ask(self._style("? ", "warn") + question, self.write)

    def wake(self) -> None:
        self.broker.push("wake")

    def prompt(self) -> None:
        with self._lock:
            self._newline()
            self.write(self._style(PROMPT, "bold"))

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
                    self.print("[aborted]", "warn")
            elif t == "tool_execution_start":
                self._block = None
                self._newline()
                self.write(f"● {ev.tool_name}", "tool")
                self.write(f"({_brief(ev.tool_name, ev.args)})\n", "dim")
            elif t == "tool_execution_end":
                text = ev.result.as_text() if ev.result else ""
                lines = text.strip().splitlines() or ["(no output)"]
                width = shutil.get_terminal_size((100, 20)).columns - 6
                first = lines[0][:width]
                more = f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
                self.write(f"  ⎿ {first}{more}\n", "error" if ev.is_error else "dim")

    def _sub(self, ev: AgentEvent) -> None:
        if ev.type == "tool_execution_start":
            self._newline()
            self.write(f"  ↳ [{ev.agent}] {ev.tool_name}({_brief(ev.tool_name, ev.args)})\n", "dim")
        elif ev.type == "message_end" and getattr(ev.message, "role", "") == "assistant":
            if ev.message.stop_reason in ("error", "refusal"):
                self.print(f"  ↳ [{ev.agent}] ✗ {ev.message.error_message}", "error")


class PrintUI(UI):
    """`-p` mode: nothing interactive; messages go to stderr, the answer to stdout."""

    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    def print(self, text: str, style: str | None = None) -> None:
        if style in ("error", "warn"):
            print(text, file=sys.stderr)

    def on_event(self, ev: AgentEvent) -> None:
        if self.json_mode:
            print(json.dumps(event_json(ev), ensure_ascii=False, default=str), flush=True)


def event_json(ev: AgentEvent) -> dict:
    from dataclasses import asdict

    d: dict = {"type": ev.type, "agent": ev.agent}
    if ev.type == "message_update" and ev.stream_event is not None:
        d["event"] = ev.stream_event.type
        if ev.stream_event.delta:
            d["delta"] = ev.stream_event.delta
    elif ev.type == "message_end" and ev.message is not None:
        d["message"] = asdict(ev.message)
    elif ev.type.startswith("tool_execution"):
        d.update(tool_call_id=ev.tool_call_id, tool_name=ev.tool_name, args=ev.args)
        if ev.result is not None:
            d.update(result=ev.result.as_text(), is_error=ev.is_error)
    return d
