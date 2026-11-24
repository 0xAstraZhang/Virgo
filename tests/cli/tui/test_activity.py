"""Run feedback follows worker lifetimes without disturbing the conversation."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from time import strftime
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from virgo.agent.types import AgentEvent
from virgo.ai.providers import faux
from virgo.ai.stream import StreamEvent
from virgo.ai.types import AssistantMessage, RawContent, TextContent, ThinkingContent
from virgo.cli.tui import activity as activity_module
from virgo.cli.tui import transcript as transcript_module
from virgo.cli.tui.activity import RunActivity
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI, RunFinished
from virgo.cli.tui.transcript import AssistantView, Transcript

from tests.support.tui import TranscriptApp, wait_for


def summaries(app):
    return [str(row.content) for row in app.transcript.query(".notice") if str(row.content).startswith("✻ ")]


def stream_event(kind, *blocks, index=0, delta="", agent="main"):
    stream = StreamEvent(kind, AssistantMessage(list(blocks)), index=index, delta=delta)
    return AgentEvent("message_update", agent=agent, stream_event=stream)


async def poll_events(app, *events):
    for event in events:
        app.ui.on_event(event)
    await app.poll()


@pytest.fixture
def live_turn(make_session, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    time = SimpleNamespace(monotonic=lambda: clock.now, strftime=strftime)
    monkeypatch.setattr(activity_module, "time", time)
    monkeypatch.setattr(transcript_module, "time", time)

    @asynccontextmanager
    async def start(size=(80, 24)):
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app._poll_timer.pause()
            app.activity.begin(clock.now, None)
            await poll_events(app, AgentEvent("agent_start"), AgentEvent("turn_start"))
            yield app, pilot, clock

    return start


def test_main_text_stall_shows_elapsed_then_clears_on_progress(live_turn):
    async def scenario():
        async with live_turn() as (app, pilot, clock):
            header = app.transcript.activity_card.thought_header

            async def assert_header(label=None):
                await pilot.pause()
                assert header.display == header.has_class("active") == (label is not None)
                lines = [strip.text for strip in app.screen._compositor.render_strips()]
                if label is None:
                    assert not any("Responding…" in line for line in lines)
                else:
                    assert label in header.content.plain
                    assert label in lines[header.region.y]

            clock.now = 101.0
            await poll_events(app, stream_event("text_delta", TextContent("a"), delta="a"))
            await assert_header()
            clock.now = 102.0
            await poll_events(app, stream_event("text_delta", TextContent("ab"), delta="b"))
            clock.now = 103.99
            await app.poll()
            await assert_header()
            clock.now = 104.0
            await app.poll()
            await assert_header("Responding… (4s)")
            clock.now = 105.0
            await app.poll()
            await assert_header("Responding… (5s)")
            await poll_events(app, stream_event("text_delta", TextContent("ab")))
            await assert_header("Responding… (5s)")

            # Consume directly so a later poll cannot mask a late hide.
            await app.transcript.consume(stream_event("text_delta", TextContent("abc"), delta="c"))
            await assert_header()
            clock.now = 107.0
            await app.poll()
            await assert_header("Responding… (7s)")
            # The bridge keeps only the last delta, which has no payload but a longer snapshot.
            await poll_events(app, stream_event("text_delta", TextContent("abcd"), delta="d"),
                              stream_event("text_delta", TextContent("abcd")))
            await assert_header()
            clock.now = 109.0
            await app.poll()
            await assert_header("Responding… (9s)")
            await poll_events(app, AgentEvent("message_end", message=AssistantMessage([TextContent("abcd")])))
            assert "Working… (9s)" in header.content.plain
            app.transcript.finish_activity()
            await assert_header()

    asyncio.run(scenario())


def test_empty_text_start_keeps_first_token_indicator(live_turn):
    async def scenario():
        async with live_turn() as (app, _, clock):
            header = app.transcript.activity_card.thought_header
            for kind in ("text_start", "text_delta"):
                await poll_events(app, stream_event(kind, TextContent("")))
                assert header.display and "Responding… (0s)" in header.content.plain
            clock.now += 0.5
            await poll_events(app, stream_event("text_delta", TextContent("first")))
            assert not header.display
            clock.now += 2
            await app.poll()
            assert header.display
            # A nonempty payload also signals progress when the snapshot is unchanged.
            await app.transcript.consume(stream_event("text_delta", TextContent("first"), delta="!"))
            assert not header.display

    asyncio.run(scenario())


def test_visible_event_headers_take_priority_over_stalled_text(live_turn):
    async def scenario():
        async with live_turn() as (app, _, clock):
            card = app.transcript.activity_card
            text = TextContent("partial")
            await poll_events(app, stream_event("text_start", text))
            search = RawContent("openai-responses", {"type": "web_search_call", "id": "a", "status": "in_progress"})
            server = RawContent("anthropic", {"type": "server_tool_use", "id": "b", "name": "web_search"})
            await poll_events(app, stream_event("native_tool_start", text, search, index=1),
                              stream_event("native_tool_start", text, search, server, index=2))
            clock.now += 3
            await app.poll()
            assert {row.call_id for row in card.active_events if row.kind == "tool_call"} == {"a", "b"}
            assert not card.thought_header.display
            search.data["status"] = "completed"
            await poll_events(app, stream_event("native_tool_end", text, search, server, index=1))
            assert not card.thought_header.display
            result = RawContent("anthropic", {"type": "web_search_tool_result", "tool_use_id": "b", "content": []})
            await poll_events(app, stream_event("native_tool_end", text, search, server, result, index=3))
            assert card.thought_header.display and "Responding… (3s)" in card.thought_header.content.plain

            # Child agent events stay isolated from the main transcript.
            child = TextContent("child text")
            await poll_events(app, stream_event("text_delta", child, agent="explorer"))
            assert card.thought_header.display
            await poll_events(app, AgentEvent("message_end", agent="explorer", message=AssistantMessage([child])))
            assert card.thought_header.display
            await poll_events(app, stream_event("thinking_start", text, search, server, result,
                                                 ThinkingContent("checking"), index=4))
            clock.now += 3
            await app.poll()
            assert not card.thought_header.display
            assert {row.kind for row in card.active_events} == {"thinking"}

    asyncio.run(scenario())


def test_waiting_overrides_fast_text(live_turn):
    async def scenario():
        async with live_turn() as (app, _, clock):
            header = app.transcript.activity_card.thought_header
            await poll_events(app, stream_event("text_delta", TextContent("first")))
            assert not header.display
            for key in ("approval", "input", "closing"):
                waiting = app.session.t("ui.state." + key)
                app.activity.waiting = waiting
                app.activity.tick()
                app.transcript.show_activity(app.activity.content, waiting=waiting)
                await app.transcript.consume(stream_event("text_delta", TextContent("first " + key), delta=key))
                assert header.display and f"{waiting}… (0s)" in header.content.plain
                clock.now += 0.1
                await app.poll()
                assert not header.display

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["aborted", "error"])
def test_stalled_text_finishes_and_next_turn_starts_cleanly(live_turn, reason):
    async def scenario():
        async with live_turn() as (app, _, clock):
            card = app.transcript.activity_card
            text = TextContent("partial")
            await poll_events(app, stream_event("text_delta", text))
            clock.now += 2
            await app.poll()
            assert card.thought_header.display
            await poll_events(app, AgentEvent("message_end", message=AssistantMessage([text], stop_reason=reason)))
            assert card._text_progress_at is None
            app.ui.push("done", RunFinished(1))
            await app.poll()
            assert not card.thought_header.display and not card.active_events

            app.activity.begin(clock.now, None)
            await poll_events(app, AgentEvent("agent_start"), AgentEvent("turn_start"),
                              stream_event("text_start", TextContent("")))
            fresh = app.transcript.activity_card
            assert fresh is not card and not card.thought_header.display
            assert fresh.thought_header.display and "Responding… (0s)" in fresh.thought_header.content.plain
            await poll_events(app, stream_event("text_delta", TextContent("recovered")))
            assert not fresh.thought_header.display

    asyncio.run(scenario())


def test_text_progress_clears_at_block_message_and_run_boundaries(live_turn):
    async def scenario():
        async with live_turn() as (app, _, clock):
            card = app.transcript.activity_card
            text = TextContent("visible")
            for end in (
                stream_event("text_end", text),
                stream_event("done", text, index=-1),
                stream_event("error", text, index=-1),
                stream_event("thinking_start", text, ThinkingContent("checking"), index=1),
                AgentEvent("message_start", message=AssistantMessage()),
            ):
                await poll_events(app, stream_event("text_delta", text, delta="visible"))
                assert card._text_progress_at == clock.now
                await poll_events(app, end)
                assert card._text_progress_at is None
            await poll_events(app, stream_event("text_delta", text))
            app.transcript.finish_activity()
            assert card._text_progress_at is None and not card.thought_header.display

    asyncio.run(scenario())


def test_replayed_stream_never_arms_text_stall():
    async def scenario():
        async with TranscriptApp().run_test():
            replay = Transcript()
            replay._staged = []
            await replay._replay([{
                "type": "custom", "kind": "transcript_stream",
                "data": {"stream_type": "text_delta", "index": 0, "block": {"type": "text", "text": "saved"}},
            }], has_run_boundaries=True)
            card = replay.activity_card
            assert card.events[0].text == "saved"
            # Inspect the staged partial before finish_activity can clear its state.
            assert card._text_progress_at is None
            assert not card.active_events and not card.thought_header.display

    asyncio.run(scenario())


def test_text_stall_preserves_narrow_screen_draft_and_manual_scroll(live_turn):
    async def scenario():
        async with live_turn(size=(40, 16)) as (app, pilot, clock):
            text = "\n\n".join(f"Paragraph {i}" for i in range(35))
            await poll_events(app, stream_event("text_delta", TextContent(text)))
            app.editor.load_text("next message")
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()
            view = app.transcript
            y = view.scroll_y
            assert 0 < y < view.max_scroll_y and not view.follow_latest
            header = view.activity_card.thought_header
            for stalled in (True, False):
                if stalled:
                    clock.now += 2
                    await app.poll()
                else:
                    await poll_events(app, stream_event("text_delta", TextContent(text + "."), delta="."))
                await pilot.pause()
                assert header.display == stalled
                if stalled:
                    assert header.region.height == 1 and header.region.width <= 40
                assert view.scroll_y == y and not view.follow_latest
                assert app.editor.text == "next message"
                assert app.editor.render_line(0).text.rstrip() == "› next message"

    asyncio.run(scenario())


def test_activity_tracks_main_phases_and_elapsed_time_across_retries(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        activity_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], strftime=lambda _: "16:44"),
    )

    activity = RunActivity()
    activity.begin(clock[0], "high")
    activity.consume(AgentEvent("agent_start"))
    assert "Thinking… (0s · thinking with high effort)" in str(activity.content)
    initial_frame = str(activity.content)[0]
    clock[0] += 0.2
    activity.tick()
    assert str(activity.content)[0] != initial_frame

    def stream(kind, *, agent="main"):
        activity.consume(
            AgentEvent("message_update", agent=agent, stream_event=StreamEvent(kind, AssistantMessage()))
        )

    clock[0] = 165.0
    stream("thinking_delta")
    assert "Thinking… (1m 5s" in str(activity.content)
    stream("text_delta", agent="explorer")
    activity.consume(
        AgentEvent("tool_execution_start", agent="explorer", tool_call_id="child", tool_name="bash")
    )
    assert "Thinking…" in str(activity.content)
    assert not activity.running_tools
    stream("text_start")  # OpenAI streams can move here without thinking_end.
    assert "Responding… (1m 5s)" in str(activity.content)
    stream("toolcall_start")
    assert "Preparing tools…" in str(activity.content)
    for call_id, name in (("one", "read"), ("two", "glob")):
        activity.consume(AgentEvent("tool_execution_start", tool_call_id=call_id, tool_name=name))
    assert "Running 2 tools…" in str(activity.content)
    activity.consume(AgentEvent("tool_execution_end", tool_call_id="one"))
    assert "Running glob…" in str(activity.content)
    activity.consume(AgentEvent("tool_execution_end", tool_call_id="two"))
    stream("retry")
    assert "Retrying… (1m 5s)" in str(activity.content)
    stream("thinking_delta")
    assert "Thinking… (1m 5s" in str(activity.content)

    activity.waiting = "Waiting for approval"
    activity.tick()
    assert "Waiting for approval… (1m 5s)" in str(activity.content)
    assert activity.finish(RunFinished(1), 172.0) == "✻ Worked for 1m 12s · done 16:44"
    frozen = str(activity.content)
    clock[0] = 900.0
    activity.tick()
    assert str(activity.content) == frozen
    assert not activity.display
    assert activity.started_at is None
    assert activity.finish(RunFinished(1), 900.0) is None


@pytest.mark.parametrize(
    ("reason", "result", "expected"),
    [
        ("stop", RunFinished(1, interrupted=True), "Interrupted"),
        ("aborted", RunFinished(1), "Interrupted"),
        ("stop", RunFinished(1, failed=True), "Failed"),
        ("error", RunFinished(1), "Failed"),
        ("refusal", RunFinished(1), "Stopped"),
        ("length", RunFinished(1), "Stopped"),
    ],
)
def test_activity_freezes_terminal_outcomes(reason, result, expected):
    activity = RunActivity()
    activity.begin(100, "medium")
    activity.consume(AgentEvent("agent_start"))
    activity.consume(AgentEvent("message_end", message=AssistantMessage(stop_reason=reason)))
    assert activity.finish(result, 103) == f"✻ {expected} after 3s"
    assert not activity.display and activity.started_at is None


def test_waiting_for_first_token_animates_and_concurrent_command_does_not_restart_it(make_session):
    entered, release = threading.Event(), threading.Event()

    def response(_context):
        entered.set()
        assert release.wait(4), "Test did not release the model response"
        return faux.reply("Ready now.")

    async def scenario():
        ui = FullscreenUI()
        session = make_session(response, ui=ui)
        app = VirgoApp(session, ui, initial="Wait for the first token")
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                # The worker can enter the provider before the UI consumes agent_start.
                await wait_for(pilot, lambda: entered.is_set() and app.transcript.activity_card is not None)
                card = app.transcript.activity_card
                await wait_for(pilot, lambda: "Thinking…" in str(card.thought_header.content))
                assert app.activity.display and "Thinking…" in str(app.activity.content)
                assert len(app.transcript.query(AssistantView)) == 1
                assert card.thought_header.display
                assert not card.thought_header.can_focus
                assert not app.transcript.query(".speaker")
                assert not app.query("#activity")
                started_at = app.activity.started_at
                first_frame = str(card.thought_header.content)[0]
                await wait_for(pilot, lambda: str(card.thought_header.content)[0] != first_frame)
                app.controller.submit("/help")
                await wait_for(pilot, lambda: bool(app.transcript.query(".notice")))
                assert app.activity.started_at == started_at
                assert app.activity.display and not summaries(app)
                release.set()
                await wait_for(pilot, lambda: len(summaries(app)) == 1)
                assert summaries(app)[0].startswith("✻ Worked for ")
                assert not app.activity.display
                finished = summaries(app)
                await pilot.pause(0.2)
                assert summaries(app) == finished
        finally:
            release.set()

    asyncio.run(scenario())


def test_queued_runs_and_automatic_wake_get_separate_summaries_but_local_commands_do_not(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("first"), faux.reply("second"), faux.reply("notification"), ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._poll_timer.pause()
            app.controller.submit("first request")
            await asyncio.to_thread(app.controller._worker.join, 1)
            assert not app.controller._worker.is_alive()
            assert app.controller.busy and not session.busy
            # The completed worker is still busy until poll; this prompt queues.
            app.controller.submit("second request")
            assert app.controller.pending == ["after this run: second request"]
            await app.poll()
            app._poll_timer.resume()
            await wait_for(pilot, lambda: len(summaries(app)) == 2 and not app.controller.busy)
            order = []
            for child in app.transcript.children:
                if child.has_class("user-message"):
                    order.append("user")
                elif child.has_class("notice") and str(child.content).startswith("✻ "):
                    order.append("summary")
            assert order == ["user", "summary", "user", "summary"]
            session.notify("background work finished")
            await wait_for(pilot, lambda: len(summaries(app)) == 3 and not app.controller.busy)
            assert all(summary.startswith("✻ Worked for ") for summary in summaries(app))
            app.controller.submit("/model")
            await wait_for(pilot, lambda: app.query_one("#preferences").display)
            await pilot.press("escape")
            # poll marks the worker idle before async transcript updates finish.
            await wait_for(
                pilot, lambda: not app.controller.busy and not app._polling and not app.activity.display
            )
            assert len(summaries(app)) == 3
            assert not app.activity.display
            assert len(faux.CALLS["test"]) == 3

    asyncio.run(scenario())


def test_failure_and_interruption_end_animation_and_next_run_starts_cleanly(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(
            faux.error("offline"), faux.reply("cancel me", delay=4), faux.reply("recovered"), ui=ui
        )
        app = VirgoApp(session, ui, initial="request that fails")
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(pilot, lambda: len(summaries(app)) == 1)
            assert summaries(app)[0].startswith("✻ Failed after ")
            assert not app.activity.display
            app.controller.submit("request to interrupt")
            # Wait for the delayed model response to be claimed. The agent and
            # activity start earlier; interrupting then leaves that reply queued
            # for the recovery request.
            await wait_for(pilot, lambda: len(faux.CALLS["test"]) == 2
                           and session.main.agent.running and app.activity.display)
            await pilot.press("escape")
            await wait_for(pilot, lambda: len(summaries(app)) == 2)
            assert summaries(app)[1].startswith("✻ Interrupted after ")
            assert app.editor.prompt_marker == "›"
            assert not app.activity.display
            app.controller.submit("try again")
            await wait_for(pilot, lambda: len(summaries(app)) == 3)
            assert summaries(app)[2].startswith("✻ Worked for ")
            assert not app.activity.display

    asyncio.run(scenario())


def test_activity_stays_in_message_on_narrow_screen_and_preserves_scroll_position(make_session):
    entered, release = threading.Event(), threading.Event()

    def response(_context):
        entered.set()
        assert release.wait(5), "Test did not release the model response"
        return faux.reply("new answer")

    async def scenario():
        ui = FullscreenUI()
        session = make_session(faux.reply("\n\n".join(f"Paragraph {i}." for i in range(40))), response, ui=ui)
        app = VirgoApp(session, ui, initial="long answer")
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await wait_for(pilot, lambda: len(summaries(app)) == 1)
                await pilot.press("ctrl+home")
                await wait_for(
                    pilot, lambda: app.transcript.scroll_y == 0 and not app.transcript.follow_latest
                )
                app._poll_timer.pause()
                app.editor.load_text("next answer")
                await pilot.press("enter")
                assert app.controller.busy and not app.activity.display
                assert app.editor.render_line(0).text.startswith("› ")
                assert "Esc interrupt" in app.query_one("#hints").render_line(0).text
                app._poll_timer.resume()
                await wait_for(pilot, lambda: entered.is_set() and app.activity.display)
                for width, height in ((40, 16), (80, 24)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause(0.15)
                    header = app.transcript.activity_card.thought_header
                    assert header.region.height == 1
                    assert header.parent is app.transcript.activity_card
                    assert header.region.width <= width
                    assert app.query_one("#status").region.bottom <= height
                    status = app.query_one("#status")
                    assert "Thinking" not in status.render_line(0).text
                    first = str(header.content)[0]
                    await wait_for(pilot, lambda first=first, header=header: str(header.content)[0] != first)
                    assert app.editor.render_line(0).text.startswith("› ")
                    assert not app.query("#activity")
                    assert app.transcript.scroll_y == 0
                    assert not app.transcript.follow_latest
                await pilot.click("#jump")
                await wait_for(pilot, lambda: app.transcript.follow_latest and
                               "Thinking" not in str(status.content))
                assert header.display and "Thinking" in str(header.content)
                await pilot.press("ctrl+home")
                release.set()
                await wait_for(pilot, lambda: len(summaries(app)) == 2)
                await pilot.pause()
                assert app.transcript.scroll_y == 0
                assert not app.transcript.follow_latest
                assert "Thinking" not in str(app.query_one("#status").content)
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("reasoning", [False, True], ids=["long-reply", "expanded-reasoning"])
def test_draft_marker_stays_static_when_event_header_scrolls_away(make_session, reasoning):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(50, 16)) as pilot:
            await pilot.pause()
            app.activity.begin(activity_module.time.monotonic(), "high")
            app.controller.busy = True
            text = "\n\n".join(f"Paragraph {i}" for i in range(35))
            message = AssistantMessage([ThinkingContent(text) if reasoning else TextContent(text)])
            await app.transcript.consume(AgentEvent("message_start", message=message))
            kind = "thinking_delta" if reasoning else "text_delta"
            await app.transcript.consume(AgentEvent("message_update", stream_event=StreamEvent(kind, message, index=0)))
            if reasoning:
                await pilot.press("ctrl+t")
            app.editor.load_text("下一条消息")
            await pilot.pause()
            row = app.transcript.activity_card.events[0]
            assert row.header.region.bottom <= app.transcript.region.y
            def visible_marker():
                region = app.editor.content_region
                return app.screen._compositor.render_strips()[region.y].crop(region.x, region.x + 1).text

            assert visible_marker() == "›"
            first = row.activity_frame
            await wait_for(pilot, lambda: row.activity_frame != first)
            assert visible_marker() == "›"
            if reasoning:
                title = row.header.content
                assert title.plain.startswith(row.activity_frame + " Thinking")
                assert str(title.spans[0].style) == app.theme_variables["accent"]
            else:
                assert not row.header.display
            assert app.editor.render_line(0).text[2:].rstrip() == app.editor.text == "下一条消息"
            app.controller.busy = False
            app.activity.reset()
            await wait_for(pilot, lambda: app.editor.render_line(0).text.startswith("› 下一条消息"))

    asyncio.run(scenario())


@pytest.mark.parametrize("spec,suffix,effort", [
    ("deepseek/deepseek-flash", "deepseek-flash · medium→high", "high"),
    ("kimi/kimi-k3.1-pro", "kimi-k3.1-pro · medium→default", "default"),
    ("faux/test", "  test", None),
])
def test_footer_and_thinking_banner_follow_model_effort(make_session, spec, suffix, effort):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, settings={"effort": "medium"})
        session.set_model(spec)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            ui.push("run_start", 1)
            ui.on_event(AgentEvent("agent_start"))
            await wait_for(pilot, lambda: "Thinking…" in str(app.activity.content))
            content = str(app.activity.content)
            if effort is None:
                assert "effort" not in content
            else:
                assert f"thinking with {effort} effort" in content
            assert str(app.query_one("#status", Static).content).endswith(suffix)
            if spec == "deepseek/deepseek-flash":
                await pilot.resize_terminal(35, 16)
                await pilot.pause()
                assert str(app.query_one("#status", Static).content).endswith("deepseek-flash · high")

    asyncio.run(scenario())
