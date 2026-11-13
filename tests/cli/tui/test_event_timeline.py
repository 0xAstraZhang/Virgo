"""Run timelines preserve arrival order while details remain opt-in."""

from __future__ import annotations

import asyncio

from virgo.agent.types import AgentEvent, ToolResult
from virgo.ai.stream import StreamEvent
from virgo.ai.types import (
    AssistantMessage,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_to_dict,
)
from virgo.cli.tui.transcript import AssistantView, EventView, ToolView, Transcript

from tests.support.tui import TranscriptApp, saved_entry


def result_message(call_id: str, text: str, name: str = "bash") -> ToolResultMessage:
    return ToolResultMessage(call_id, name, [TextContent(text)])


def saved_result(message, agent: str = "main") -> dict:
    return {
        "type": "custom",
        "kind": "transcript_tool_result",
        "data": {"agent": agent, "message": message_to_dict(message)},
    }


def test_parallel_results_notifications_and_agents_share_arrival_order():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(100, 40)) as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("agent_start"))
            await view.consume(AgentEvent("turn_start"))
            await view.consume(AgentEvent("message_end", message=UserMessage("Run both tools")))
            await view.consume(AgentEvent("message_end", message=AssistantMessage([
                ThinkingContent("Check both"),
                TextContent("Starting the checks"),
                ToolCall("a", "bash", {"command": "slow"}),
                ToolCall("b", "bash", {"command": "fast"}),
            ], stop_reason="tool_use")))
            for call_id in ("a", "b"):
                await view.consume(AgentEvent("tool_execution_start", tool_call_id=call_id, tool_name="bash"))
            await view.consume(AgentEvent(
                "tool_execution_end", tool_call_id="b", tool_name="bash", result=ToolResult.text("B finished")
            ))
            await view.consume(AgentEvent(
                "message_end", message=UserMessage("Background news", kind="notification")
            ))
            await view.consume(AgentEvent("message_end", agent="explorer", message=AssistantMessage([
                ThinkingContent("Inspect child work"), TextContent("Child observation")
            ])))
            await view.consume(AgentEvent(
                "tool_execution_end", tool_call_id="a", tool_name="bash", result=ToolResult.text("A finished")
            ))
            # The later model-history records must not reorder or duplicate rows.
            for call_id in ("a", "b"):
                await view.consume(AgentEvent("message_end", message=result_message(call_id, call_id.upper() + " finished")))
            await view.consume(AgentEvent(
                "message_end", message=AssistantMessage([TextContent("Both checks passed")])
            ))
            await pilot.pause()
            runs = list(view.query(AssistantView))
            assert len(runs) == 1
            run = runs[0]
            assert [(event.kind, event.agent) for event in run.events] == [
                ("thinking", "main"), ("text", "main"),
                ("tool_call", "main"), ("tool_call", "main"),
                ("tool_result", "main"), ("notification", "main"),
                ("thinking", "explorer"), ("text", "explorer"),
                ("tool_result", "main"), ("text", "main"),
            ]
            assert [event.call_id for event in run.events if isinstance(event, ToolView)] == ["a", "b", "b", "a"]
            assert run.events[4] is view.results[("main", "b")]
            assert run.events[8] is view.results[("main", "a")]
            assert view.tools[("main", "a")] is not view.results[("main", "a")]
            assert all(event.parent is run.thought_body for event in run.events)
            assert run.thought_body.display
            assert all(not event.expanded and not event.output.display for event in run.events if event.kind != "text")
            assert run.body.display and run.body._markdown == "Both checks passed"
            assert not run.events[-1].display
            assert all(event.display for event in run.events[:-1])
            assert len(view.query(".user-message")) == 1

    asyncio.run(scenario())


def test_streamed_blocks_update_in_place_without_losing_interleaved_events():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("message_start", message=AssistantMessage()))
            await view.consume(AgentEvent("message_update", stream_event=StreamEvent(
                "thinking_delta", AssistantMessage([ThinkingContent("First")]), index=0
            )))
            await view.consume(AgentEvent("message_end", message=UserMessage("News", kind="notification")))
            partial = AssistantMessage([ThinkingContent("First and second"), TextContent("Answer")])
            await view.consume(AgentEvent("message_update", stream_event=StreamEvent("text_delta", partial, index=1)))
            await view.consume(AgentEvent("message_end", message=partial))
            await pilot.pause()
            run = view.query_one(AssistantView)
            assert [(event.kind, event.text) for event in run.events] == [
                ("thinking", "First and second"), ("notification", "News"), ("text", "Answer")
            ]
            assert run.body._markdown == "Answer"

    asyncio.run(scenario())


def test_anthropic_native_search_and_sources_are_folded_events():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            message = AssistantMessage([
                RawContent("anthropic", {
                    "type": "server_tool_use", "id": "native", "name": "web_search", "input": {"query": "release"},
                }),
                RawContent("anthropic", {
                    "type": "web_search_tool_result", "tool_use_id": "native",
                    "content": [{"type": "web_search_result", "url": "https://example.com", "title": "Release"}],
                }),
                TextContent("A new release is available."),
                TextContent("\n\nSources:\n- Release: https://example.com"),
            ])
            await view.consume(AgentEvent("message_end", message=message))
            await pilot.pause()
            run = view.query_one(AssistantView)
            assert [event.kind for event in run.events] == ["tool_call", "tool_result", "text", "sources"]
            assert run.events[0].tool_name == run.events[1].tool_name == "web_search"
            assert "https://example.com" in run.events[1].text
            assert run.thought_body.display
            assert all(not event.output.display for event in run.events if event.kind != "text")
            assert run.body._markdown == "A new release is available."
            run.set_thinking(True)
            await pilot.pause()
            assert all(event.size.height == 1 for event in run.events if event.display)
            assert run.events[3].display

    asyncio.run(scenario())


def test_responses_native_completion_adds_one_result_before_final_text():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("message_start", message=AssistantMessage()))
            for status in ("in_progress", "searching", "completed"):
                message = AssistantMessage([RawContent("openai-responses", {
                    "type": "web_search_call", "id": "search-1", "status": status,
                    "action": {"type": "search", "query": "news"},
                })])
                await view.consume(AgentEvent("message_update", stream_event=StreamEvent("native_tool_end", message, index=0)))
            message.content.extend([TextContent("Search answer"), TextContent("\n\nSources:\nhttps://example.com")])
            await view.consume(AgentEvent("message_end", message=message))
            await pilot.pause()
            run = view.query_one(AssistantView)
            assert [event.kind for event in run.events] == ["tool_call", "tool_result", "text", "sources"]
            assert len(run.query(ToolView)) == 2
            assert run.events[1].status == "done"
            assert run.body._markdown == "Search answer"

    asyncio.run(scenario())


def test_collapsed_events_fit_one_line_and_keep_bold_names_after_resizing():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("message_end", message=AssistantMessage([
                TextContent("A very long\nmessage with 中文 " * 20),
                ToolCall("search", "web_search", {"query": "中文 and more words " * 30}),
            ], stop_reason="tool_use")))
            await view.consume(AgentEvent("message_end", message=result_message("search", "Long result\n" * 40, "web_search")))
            run = view.query_one(AssistantView)
            run.set_thinking(True)
            for width in (80, 32, 120):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                assert not run.thought_header.display
                for event in run.events:
                    if event.kind == "text":
                        assert not event.header.display
                        assert event.output.display
                        assert event.size.height > 1
                        continue
                    assert isinstance(event, EventView)
                    assert event.size.height == event.header.size.height == 1
                    assert not event.output.display
                    title = event.header.content
                    assert "\n" not in title.plain
                    assert title.cell_len <= event.header.size.width
                    assert title.plain.endswith("...")
                    if event.tool_name:
                        assert any(str(span.style) == "bold" and title.plain[span.start:span.end] == "web_search" for span in title.spans)

    asyncio.run(scenario())


def test_web_search_result_body_click_collapses_only_result():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("message_end", message=AssistantMessage([
                ToolCall("search", "web_search", {"query": "release"})
            ], stop_reason="tool_use")))
            await view.consume(AgentEvent("message_end", message=result_message(
                "search", "Release title\nhttps://example.com\nFull search snippet", "web_search"
            )))
            run = view.query_one(AssistantView)
            result = view.results[("main", "search")]
            run.set_thinking(True)
            await pilot.pause()
            await pilot.click(result.header, offset=(5, 0))
            await pilot.pause(app.CLICK_CHAIN_TIME_THRESHOLD + 0.05)
            assert result.expanded and result.output.display
            assert run.show_thinking and run.thought_body.display
            await pilot.click(result.output, offset=(6, 1))
            await pilot.pause(app.CLICK_CHAIN_TIME_THRESHOLD + 0.05)
            assert not result.expanded and not result.output.display
            assert run.show_thinking and run.thought_body.display
            assert result.size.height == 1

    asyncio.run(scenario())


def test_history_custom_results_preserve_completion_order_without_duplicates():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            result_a, result_b = result_message("a", "A"), result_message("b", "B")
            entries = [
                saved_entry(UserMessage("Check A and B")),
                saved_entry(AssistantMessage([
                    ToolCall("a", "bash", {"command": "slow"}), ToolCall("b", "bash", {"command": "fast"}),
                ], stop_reason="tool_use")),
                saved_result(result_b),
                saved_result(result_a),
                saved_entry(result_a),
                saved_entry(result_b),
                saved_entry(AssistantMessage([TextContent("Complete")])),
            ]
            await view.load_history(entries)
            await pilot.pause()
            run = view.query_one(AssistantView)
            assert [(row.kind, row.call_id) for row in run.events if isinstance(row, ToolView)] == [
                ("tool_call", "a"), ("tool_call", "b"), ("tool_result", "b"), ("tool_result", "a")
            ]
            assert run.body._markdown == "Complete"
            assert all(not event.expanded for event in run.events)
            assert view.activity_card is None
            await view.load_history(entries)
            await pilot.pause()
            assert len(view.query(AssistantView)) == 1
            assert len(view.query(ToolView)) == 4

    asyncio.run(scenario())


def test_live_messages_are_visible_and_folds_are_independent():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app.query_one(Transcript)
            message = AssistantMessage([
                ThinkingContent(""),
                TextContent("Checking **sources**.\n\nPlease wait."),
                ToolCall("search", "web_search", {"query": "release"}),
            ], stop_reason="tool_use")
            await view.consume(AgentEvent("message_start", message=message))
            await pilot.pause()
            run = view.query_one(AssistantView)
            thought, progress, tool = run.events
            assert not thought.display
            assert progress.display and progress.output.display
            assert not progress.header.display
            assert progress.output._markdown == message.content[1].text
            assert tool.display and not tool.output.display
            message.content[0].thinking = "Compare official sources"
            await view.consume(AgentEvent("message_update", stream_event=StreamEvent("thinking_delta", message, 0)))
            await pilot.pause()
            assert thought.display and not thought.output.display
            thought.header.focus()
            await pilot.press("enter")
            assert thought.output.display and not tool.output.display
            tool.header.focus()
            await pilot.press("enter")
            assert thought.output.display and tool.output.display
            view.toggle_thinking()
            view.toggle_thinking()
            assert not thought.output.display and tool.output.display
            assert progress.output.display and run.thought_body.display
            await view.consume(AgentEvent("message_end", message=message))
            await view.consume(AgentEvent("message_start", message=AssistantMessage([TextContent("Done")])))
            assert progress.display and progress.output.display
    asyncio.run(scenario())
