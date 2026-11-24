"""Plain REPL scheduling, driven in-process through a scripted line source."""

from __future__ import annotations

import threading

from virgo.ai.providers import faux
from virgo.cli import repl

from tests.support.harness import ReplUI, ScriptedBroker, calls, wait_until


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
    # /exit aborts active work, so wait for the deferred prompt's reply before exiting.
    wait_until(lambda: session.main.agent.last_text == "answer")
    broker.push("line", "/exit")
    runner.join(5)

    assert not runner.is_alive()
    assert calls()[0]["messages"][-1]["content"] == "what is up"
    assert session.main.agent.last_text == "answer" and session.main.agent.drain_queues() == []
    assert "[steering message queued]" not in ui.text


def test_failing_inline_command_does_not_end_the_repl(make_session, monkeypatch):
    monkeypatch.setattr(repl, "_setup_readline", lambda *args: None)
    ui, broker = ReplUI(), ScriptedBroker()
    session = make_session(ui=ui)

    def broken(s, args):
        raise RuntimeError("status unavailable")

    session.commands.register("broken-status", broken, concurrent=True)
    runner = threading.Thread(target=repl.run_repl, args=(session, ui, broker), daemon=True)
    runner.start()
    broker.push("line", "/broken-status")
    wait_until(lambda: "status unavailable" in ui.text)
    assert runner.is_alive()
    broker.push("line", "/exit")
    runner.join(5)
    assert not runner.is_alive()


def test_double_slash_input_is_steered_as_prompt_not_queued_as_command(make_session):
    session = make_session()
    steered = []
    session.steer = lambda text: (steered.append(text), True)[1]
    assert repl.classify_input(session, "//not-a-command", busy=True) == ("steer", None)
    assert steered == ["//not-a-command"]
    assert repl.classify_input(session, "/effort high", busy=True) == ("defer_command", None)


def test_welcome_shows_effective_effort(make_session, monkeypatch):
    monkeypatch.setattr(repl, "_setup_readline", lambda *_: None)
    ui, broker = ReplUI(), ScriptedBroker()
    session = make_session(ui=ui, settings={"effort": "medium"})
    session.set_model("deepseek/deepseek-flash")
    broker.push("line", "/exit")
    repl.run_repl(session, ui, broker)
    assert "effort medium→high" in ui.text
