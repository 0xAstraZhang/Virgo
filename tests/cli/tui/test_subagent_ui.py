"""Sub-agent execution stays out of the main TUI transcript and opens in a trace modal."""

from __future__ import annotations

import asyncio
import threading

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.subagents import SubagentPicker, SubagentTraceScreen
from virgo.cli.tui.transcript import EventView, ToolView

from tests.support.tui import has_reply, wait_for


def test_subagent_events_isolated_from_main_transcript_and_viewable_in_modal(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True, settings={"language": "en"})
        faux.push(
            "test",
            faux.reply(tools=[("agent", {"description": "Inspect workspace", "prompt": "Find python files"})]),
            faux.reply("Subagent final report", thinking="Private child reasoning", tools=[("glob", {"pattern": "*.py"})]),
            faux.reply("Subagent final report"),
            faux.reply("Main finished delegating."),
        )
        app = VirgoApp(session, ui, initial="Delegate inspection")
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(
                pilot,
                lambda: has_reply(app, "Main finished delegating.") and not app.controller.busy,
            )

            # Main transcript must not contain child events or redundant agent notices.
            main_events = list(app.transcript.query(EventView))
            assert all(ev.agent == "main" for ev in main_events)
            assert not any("Private child reasoning" in ev.text for ev in main_events)
            assert not any(isinstance(ev, ToolView) and ev.tool_name == "glob" for ev in main_events)
            assert not any(ev.kind == "notice" and "subagent-1" in ev.text for ev in main_events)

            # Main transcript has a single concise `agent` tool row bound to subagent-1.
            agent_tools = [t for t in app.transcript.query(ToolView) if t.tool_name == "agent"]
            assert len(agent_tools) == 1
            agent_row = agent_tools[0]
            assert agent_row.status == "done"
            assert agent_row.subagent_id == "subagent-1"
            assert "Inspect workspace" in agent_row.header.content.plain
            assert not agent_row.trace_link.display

            # Expanding the agent row reveals the trace link; activating it opens the modal.
            agent_row.action_toggle()
            await pilot.pause()
            assert agent_row.expanded
            assert agent_row.trace_link.display
            assert "subagent-1" in agent_row.trace_link.content.plain

            agent_row.trace_link.action_open_trace()
            await wait_for(pilot, lambda: isinstance(app.screen, SubagentTraceScreen) and app.screen._loaded)

            trace_screen = app.screen
            assert isinstance(trace_screen, SubagentTraceScreen)
            assert trace_screen.agent_id == "subagent-1"
            trace_events = list(trace_screen.trace_transcript.query(EventView))
            assert any(isinstance(ev, ToolView) and ev.tool_name == "glob" for ev in trace_events)
            assert any(ev.kind == "thinking" and "Private child reasoning" in ev.text for ev in trace_events)

            # Escape closes the modal and returns to the main screen.
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.has_modal)

    asyncio.run(scenario())


def test_subagents_command_opens_picker_and_navigates_to_trace(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True, settings={"language": "en"})
        faux.push(
            "test",
            faux.reply(tools=[("agent", {"description": "Check docs", "prompt": "Read README"})]),
            faux.reply("Docs look good."),
            faux.reply("Done checking docs."),
        )
        app = VirgoApp(session, ui, initial="Check docs")
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(pilot, lambda: has_reply(app, "Done checking docs.") and not app.controller.busy)

            app.editor.load_text("/subagents")
            app.editor.action_submit()
            await wait_for(pilot, lambda: isinstance(app.screen, SubagentPicker) and bool(app.screen._ids))

            picker = app.screen
            assert isinstance(picker, SubagentPicker)
            assert picker._ids == ["subagent-1"]
            await pilot.press("enter")
            await wait_for(pilot, lambda: isinstance(app.screen, SubagentTraceScreen) and app.screen._loaded)
            assert app.screen.agent_id == "subagent-1"

            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.has_modal)

    asyncio.run(scenario())


def test_background_subagent_row_stays_running_until_completion_and_streams_into_modal(make_session):
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        main_replies = iter([
            faux.reply(tools=[("agent", {"description": "Slow audit", "prompt": "Audit repo", "background": True})]),
            faux.reply("Started background audit."),
            faux.reply("Audit notification received."),
        ])

        def respond(ctx):
            if "You are a Virgo sub-agent" in ctx.system:
                started.set()
                release.wait(timeout=5)
                return faux.reply("Background answer")
            return next(main_replies)

        ui = FullscreenUI()
        session = make_session(ui=ui, persist=True, settings={"language": "en"})
        faux.handler("test", respond)
        app = VirgoApp(session, ui, initial="Run background audit")
        async with app.run_test(size=(100, 32)) as pilot:
            try:
                await wait_for(
                    pilot,
                    lambda: has_reply(app, "Started background audit.") and started.is_set(),
                )
                agent_row = next(t for t in app.transcript.query(ToolView) if t.tool_name == "agent")
                assert agent_row.subagent_id == "subagent-1"
                assert agent_row.status == "running"

                # Open trace modal while sub-agent is still running.
                app.open_subagent_trace("subagent-1")
                await wait_for(pilot, lambda: isinstance(app.screen, SubagentTraceScreen) and app.screen._loaded)
                assert app.screen.status == "running"

                release.set()
                await wait_for(pilot, lambda: app.screen.status == "done")
                await pilot.press("escape")
                await wait_for(pilot, lambda: not app.has_modal)
                await wait_for(pilot, lambda: agent_row.status == "done")
            finally:
                release.set()

    asyncio.run(scenario())


def test_background_subagent_completion_before_and_after_ack_stays_terminal():
    from virgo.agent.types import AgentEvent, ToolResult
    from virgo.ai.types import AssistantMessage, TextContent, ToolCall, ToolResultMessage
    from virgo.cli.tui.transcript import Transcript

    from tests.support.tui import TranscriptApp

    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(100, 32)) as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("agent_start"))
            await view.consume(AgentEvent("turn_start"))
            await view.consume(
                AgentEvent(
                    "message_end",
                    message=AssistantMessage(
                        [ToolCall("call-bg", "agent", {"description": "Fast bg", "prompt": "hi", "background": True})],
                        stop_reason="tool_use",
                    ),
                )
            )
            await view.consume(
                AgentEvent(
                    "tool_execution_start",
                    tool_call_id="call-bg",
                    tool_name="agent",
                    args={"description": "Fast bg", "prompt": "hi", "background": True},
                )
            )
            # Worker starts and finishes before tool_execution_end delivers the background ack.
            view.on_subagent_started({"agent_id": "subagent-1", "parent_tool_call_id": "call-bg"})
            view.on_subagent_finished({"agent_id": "subagent-1", "parent_tool_call_id": "call-bg"}, status="done")

            ack_details = {"agent_id": "subagent-1", "job_id": "job1", "background": True}
            await view.consume(
                AgentEvent(
                    "tool_execution_end",
                    tool_call_id="call-bg",
                    tool_name="agent",
                    result=ToolResult.text("Started background agent job1", details=ack_details),
                )
            )
            row = next(t for t in view.query(ToolView) if t.call_id == "call-bg")
            assert row.status == "done"
            assert not row.background_active

            # Repeated ToolResultMessage in model history must not revert terminal status to running.
            await view.consume(
                AgentEvent(
                    "message_end",
                    message=ToolResultMessage(
                        "call-bg",
                        "agent",
                        [TextContent("Started background agent job1")],
                        details=ack_details,
                    ),
                )
            )
            await pilot.pause()
            assert row.status == "done"
            assert not row.background_active

    asyncio.run(scenario())
