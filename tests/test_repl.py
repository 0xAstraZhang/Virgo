"""Plain REPL scheduling, driven in-process through a scripted line source."""

from __future__ import annotations

import queue
import threading

from virgo.ai.providers import faux
from virgo.cli import repl

from .conftest import RecordingUI, calls


class ScriptedBroker:
    """Stands in for the terminal reader: the test decides when each line arrives."""

    def __init__(self):
        self.events: queue.Queue = queue.Queue()

    def push(self, *item):
        self.events.put(item)

    def start_reader(self):
        pass

    resume_reading = cancel_pending = close = start_reader


class ReplUI(RecordingUI):
    def prompt(self):
        pass


def test_text_typed_while_a_command_runs_becomes_the_next_prompt(make_session, monkeypatch):
    monkeypatch.setattr(repl, "_setup_readline", lambda *args: None)
    ui, broker = ReplUI(), ScriptedBroker()
    session = make_session(faux.reply("answer"), ui=ui)
    started, release = threading.Event(), threading.Event()
    session.commands.register("slow", lambda s, args: (started.set(), release.wait(5)))
    runner = threading.Thread(target=repl.run_repl, args=(session, ui, broker), daemon=True)
    runner.start()

    broker.push("line", "/slow")  # e.g. a long /compact: the worker is busy, but no agent run is active
    assert started.wait(5)
    broker.push("line", "what is up")
    release.set()
    # It used to be placed in the agent's steering queue, which only a run drains: never answered.
    for _ in range(100):
        if calls():
            break
        threading.Event().wait(0.05)
    broker.push("line", "/exit")
    runner.join(5)

    assert not runner.is_alive()
    assert calls()[0]["messages"][-1]["content"] == "what is up"
    assert session.main.agent.last_text == "answer" and session.main.agent.drain_queues() == []
    assert "[steering message queued]" not in ui.text
