"""Exercise real harness scheduling without a terminal or model credentials."""

from __future__ import annotations

import threading
import time

from virgo.agent.types import AgentEvent
from virgo.ai.providers import faux
from virgo.ai.stream import StreamEvent
from virgo.ai.types import AssistantMessage, TextContent, ToolResultMessage, UserMessage
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.controller import RunController
from virgo.features.permission import ReviewDecision

from tests.support.harness import wait_until
from tests.support.tui import pump


def test_bridge_snapshots_partial_message_before_provider_mutates_it():
    ui = FullscreenUI()
    message = AssistantMessage(content=[TextContent("first")])
    stream = StreamEvent("text_delta", message, 0, "first")
    ui.on_event(AgentEvent("message_update", message=message, stream_event=stream))
    message.content[0].text += " second"
    stream.delta = " second"
    event = ui.drain()[0].payload
    assert event.message.text() == "first"
    assert event.stream_event.delta == "first"
    assert event.stream_event.partial.text() == "first"


def test_concurrent_approvals_can_be_answered_independently_and_only_once():
    ui = FullscreenUI()
    answers = {}
    workers = [
        threading.Thread(target=lambda name=name: answers.update({name: ui.confirm(f"Allow {name}?", name)}))
        for name in ("main", "helper")
    ]
    for worker in workers:
        worker.start()
    requests = []

    def both_arrived():
        requests.extend(update.payload for update in ui.drain() if update.kind == "confirmation")
        return len(requests) == 2

    wait_until(both_arrived)
    by_agent = {request.agent: request for request in requests}
    assert ui.resolve_confirmation(by_agent["helper"].id, "a")
    assert ui.resolve_confirmation(by_agent["main"].id, "n")
    assert not ui.resolve_confirmation(by_agent["main"].id, "y")
    for worker in workers:
        worker.join(1)
    assert answers == {"helper": "a", "main": "n"}


def test_stream_frames_coalesce_without_crossing_retry_or_agent_boundaries():
    ui = FullscreenUI()

    def update(text, agent="main", kind="text_delta"):
        message = AssistantMessage(content=[TextContent(text)])
        ui.on_event(
            AgentEvent(
                "message_update", agent, message=message, stream_event=StreamEvent(kind, message, 0, text)
            )
        )

    update("a")
    update("ab")
    update("", kind="retry")
    update("new")
    update("new attempt")
    update("helper text", agent="helper")
    update("new attempt continued")
    events = [item.payload for item in ui.drain()]
    assert [(ev.agent, ev.stream_event.type, ev.message.text()) for ev in events] == [
        ("main", "text_delta", "ab"),
        ("main", "retry", ""),
        ("main", "text_delta", "new attempt"),
        ("helper", "text_delta", "helper text"),
        ("main", "text_delta", "new attempt continued"),
    ]


def test_shared_cancel_signal_releases_subagent_approval():
    ui = FullscreenUI()
    signal = threading.Event()
    ui.bind_signal("explore#1", signal)
    answers = []
    worker = threading.Thread(target=lambda: answers.append(ui.confirm("Allow?", "explore#1")))
    worker.start()
    wait_until(lambda: bool(ui._waiting))
    signal.set()
    worker.join(1)
    assert answers == [None]
    assert ui.confirm("Allow another?", "explore#1") is None
    assert any(update.kind == "confirmation_cancelled" for update in ui.drain())


def test_close_releases_waiters_and_refuses_new_requests():
    ui = FullscreenUI()
    answers = []
    worker = threading.Thread(target=lambda: answers.append(ui.confirm("Allow?")))
    worker.start()
    wait_until(lambda: bool(ui._waiting))
    ui.close()
    worker.join(1)
    assert answers == [None]
    assert ui.confirm("New request") is None


def test_fast_initial_prompt_keeps_reset_history_separate(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("hello"), ui=ui)
    ui.print("startup extension output")
    controller = RunController(session, ui)
    controller.start("hi")
    updates = pump(controller)
    assert updates[0].kind == "reset" and updates[0].payload == []
    assert updates[1].kind == "print" and updates[1].payload[0] == "startup extension output"
    ended = [u.payload.message for u in updates if u.kind == "event" and u.payload.type == "message_end"]
    assert [message.text() for message in ended] == ["hi", "hello"]


def test_busy_input_steers_commands_defer_and_readonly_commands_continue(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("first", delay=0.12), faux.reply("steered"), ui=ui)
    controller = RunController(session, ui)
    controller.start("start")
    wait_until(lambda: session.main.agent.running)
    controller.submit("also read this")
    controller.submit("/model faux/next")
    controller.submit("/session")
    assert controller.pending == ["steering: also read this", "/model faux/next"]
    updates = pump(controller, lambda c: not c.busy and not c.pending and not c._helpers)
    assert session.model.spec == "faux/next"
    assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == [
        "start",
        "also read this",
    ]
    assert any(u.kind == "print" and "session " in u.payload[0] for u in updates)
    assert len(faux.CALLS["test"]) == 2


def test_input_after_worker_finished_before_poll_is_scheduled(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("first answer"), faux.reply("second answer"), ui=ui)
    controller = RunController(session, ui)
    controller.start("first")
    controller._worker.join(timeout=2)
    assert controller.busy and not session.busy and not session.main.agent.running
    controller.submit("second")
    pump(controller)
    assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == ["first", "second"]
    assert session.main.agent.last_text == "second answer"
    assert not controller.pending and not session.main.agent.drain_queues()


def test_input_before_run_starts_is_processed_as_a_later_prompt(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("first answer"), faux.reply("second answer"), ui=ui)
    entered, release = threading.Event(), threading.Event()
    inputs = []

    def block_first_input(text, _session):
        inputs.append(text)
        if text == "first":
            entered.set()
            release.wait(2)
        return text

    session.bus.on("input", block_first_input)
    controller = RunController(session, ui)
    controller.start("first")
    assert entered.wait(1)
    assert controller.busy and not session.busy
    controller.submit("second")
    release.set()
    pump(controller)
    assert inputs == ["first", "second"]
    assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == ["first", "second"]
    assert not controller.pending and not session.main.agent.drain_queues()


def test_input_during_new_session_command_preserves_fifo_after_reset(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("old answer"), faux.reply("new answer"), ui=ui)
    controller = RunController(session, ui)
    controller.start("old question")
    pump(controller)
    entered, release = threading.Event(), threading.Event()
    new_command = session.commands.get("new").fn

    def blocked_new(s, args):
        entered.set()
        release.wait(2)
        new_command(s, args)

    session.commands.register("new", blocked_new, "start a new session")
    controller.submit("/new")
    assert entered.wait(1)
    assert controller.busy and not session.busy
    controller.submit("new question")
    controller.submit("/model faux/after")
    controller.submit("last question")
    release.set()
    updates = pump(controller)
    assert any(u.kind == "reset" and not u.payload for u in updates)
    assert [m.text() for m in session.main.messages if isinstance(m, UserMessage)] == [
        "new question",
        "last question",
    ]
    assert session.model.spec == "faux/after"
    assert len(faux.CALLS["test"]) == 2 and len(faux.CALLS["after"]) == 1
    assert not controller.pending and not session.main.agent.drain_queues()


def test_background_notification_wakes_idle_session(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("notification received"), ui=ui)
    controller = RunController(session, ui)
    controller.start()
    session.notify("a task finished", source="test")
    updates = pump(controller)
    assert session.main.agent.last_text == "notification received"
    assert not session.wakeups
    assert any(u.kind == "event" and u.payload.type == "message_end" for u in updates)


def test_abort_during_permission_does_not_ask_for_remaining_tools(make_session, project, monkeypatch):
    monkeypatch.setattr(
        "virgo.features.permission.review_tool_call",
        lambda *_: ReviewDecision("ask", "external file write needs approval"),
    )
    first, second = project.parent / "one.txt", project.parent / "two.txt"
    ui = FullscreenUI()
    session = make_session(
        faux.reply(
            tools=[
                ("write", {"path": str(first), "content": "one"}),
                ("write", {"path": str(second), "content": "two"}),
            ]
        ),
        ui=ui,
        mode="auto-review",
    )
    controller = RunController(session, ui)
    controller.start("write two files")
    before = pump(controller, lambda c: bool(ui._waiting))
    controller.abort()
    after = pump(controller)
    confirmations = [u for u in [*before, *after] if u.kind == "confirmation"]
    assert len(confirmations) == 1
    assert not first.exists()
    assert not second.exists()
    results = [m for m in session.main.messages if isinstance(m, ToolResultMessage)]
    assert len(results) == 2 and all(m.is_error for m in results)
    assert "aborted" in results[1].text().lower()


def test_interrupt_before_agent_starts_survives_signal_reset(make_session):
    ui = FullscreenUI()
    session = make_session(ui=ui)
    entered, release = threading.Event(), threading.Event()

    def before_start(runtime, prompts):
        entered.set()
        release.wait(2)

    session.bus.on("before_agent_start", before_start)
    controller = RunController(session, ui)
    controller.start("must be interrupted")
    assert entered.wait(1)
    controller.abort()
    release.set()
    pump(controller)
    assert not faux.CALLS["test"]


def test_shutdown_is_nonblocking_once_and_prevents_further_work(make_session):
    ui = FullscreenUI()
    session = make_session(ui=ui)
    controller = RunController(session, ui)
    controller.start()
    entered, release = threading.Event(), threading.Event()
    shutdowns = []

    def slow_shutdown(_session):
        shutdowns.append(True)
        entered.set()
        release.wait(2)

    session.bus.on("session_shutdown", slow_shutdown)
    started = time.monotonic()
    controller.request_close()
    assert time.monotonic() - started < 0.2
    assert entered.wait(1)
    controller.request_close()
    controller.submit("must not run")
    session.notify("must not wake")
    assert ui.confirm("must not wait") is None
    assert not session.wakeups and not faux.CALLS["test"]
    assert not any(u.kind == "closed" for u in controller.poll())
    release.set()
    updates = pump(controller, lambda c: c.closed)
    session.shutdown()
    assert shutdowns == [True]
    assert sum(u.kind == "closed" for u in updates) == 1


def test_new_session_resets_transcript_and_worker_error_is_visible(make_session):
    ui = FullscreenUI()
    session = make_session(faux.reply("first answer"), ui=ui)
    controller = RunController(session, ui)
    controller.start("first question")
    pump(controller)
    controller.submit("/new")
    updates = pump(controller)
    assert any(u.kind == "reset" and not u.payload for u in updates)
    assert not session.main.messages
    session.commands.register("broken", lambda s, a: 1 / 0, "test error")
    controller.submit("/broken")
    updates = pump(controller)
    assert any(u.kind == "print" and "ZeroDivisionError" in u.payload[0] for u in updates)
    assert not controller.busy
