"""Frame batching preserves native tool results relative to streamed messages."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict

from virgo.agent.types import AgentEvent
from virgo.ai.providers.responses import parse_events
from virgo.ai.stream import StreamEvent
from virgo.ai.types import (
    AssistantMessage,
    Model,
    RawContent,
    StreamOptions,
    TextContent,
    UserMessage,
    message_to_dict,
)
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.transcript import AssistantView, Transcript

from tests.support.tui import TranscriptApp


def test_bridge_only_coalesces_matching_token_deltas():
    ui = FullscreenUI()
    updates = [
        ("text_delta", 0, "a"), ("text_delta", 0, "ab"),
        ("text_delta", 1, "next block"),
        ("thinking_delta", 1, "thought"), ("thinking_delta", 1, "more thought"),
        ("toolcall_delta", 1, "{"), ("toolcall_delta", 1, "{}"),
        ("native_tool_delta", 2, "in_progress"), ("native_tool_delta", 2, "completed"),
        ("text_end", 1, ""), ("text_start", 3, ""), ("text_delta", 3, "answer"),
    ]
    for kind, index, delta in updates:
        ui.on_event(AgentEvent("message_update", stream_event=StreamEvent(kind, AssistantMessage(), index, delta)))
    actual = [update.payload.stream_event for update in ui.drain()]
    assert [(event.type, event.index, event.delta) for event in actual] == [
        updates[i] for i in (1, 2, 4, 6, 7, 8, 9, 10, 11)
    ]


def test_native_search_completion_stays_after_preceding_message_in_one_frame():
    search = {"type": "web_search_call", "id": "ws_1", "status": "in_progress"}
    completed = {**search, "status": "completed", "action": {"type": "search", "query": "Virgo"}}
    wire = [
        {"type": "response.output_item.added", "output_index": 0, "item": search},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "Checking the release"},
        {"type": "response.output_item.done", "output_index": 0, "item": completed},
        {"type": "response.output_text.delta", "output_index": 1, "delta": " now."},
    ]
    ui = FullscreenUI()
    for event in parse_events(wire, Model("openai", "test", "openai-responses"), AssistantMessage(), StreamOptions()):
        if event.type not in ("start", "done", "error"):
            ui.on_event(AgentEvent("message_update", stream_event=event))
    updates = ui.drain()

    async def scenario():
        app = TranscriptApp()
        async with app.run_test():
            transcript = app.query_one(Transcript)
            for update in updates:
                await transcript.consume(update.payload)
            rows = transcript.activity_card.events
            assert [row.kind for row in rows] == ["tool_call", "text", "tool_result"]
            assert rows[1].text == "Checking the release now."
            assert rows[2].tool_name == "web_search"
            assert all(not row.expanded for row in rows)

    asyncio.run(scenario())


def _stream_entry(event):
    stream = event.stream_event
    return {
        "type": "custom", "kind": "transcript_stream",
        "data": {
            "agent": event.agent, "stream_type": stream.type, "index": stream.index,
            "block": asdict(stream.partial.content[stream.index]) if stream.index >= 0 else None,
        },
    }


def _assert_history_matches_live(events, message, expected_kinds):
    prompt = UserMessage("Search for the releases")
    entries = [
        {"type": "custom", "kind": "transcript_run_start", "data": {"agent": "main"}},
        {"type": "message", "message": message_to_dict(prompt)},
        *[_stream_entry(event) for event in events],
        {"type": "message", "message": message_to_dict(message)},
        {"type": "custom", "kind": "transcript_run_end", "data": {"agent": "main"}},
    ]

    def state(transcript):
        card = transcript.query_one(AssistantView)
        return card.text, [(event.kind, event.tool_name, event.text, event.display) for event in card.events]

    async def scenario():
        app = TranscriptApp()
        async with app.run_test():
            transcript = app.query_one(Transcript)
            await transcript.consume(AgentEvent("agent_start"))
            await transcript.consume(AgentEvent("turn_start"))
            await transcript.consume(AgentEvent("message_end", message=prompt))
            for event in events:
                await transcript.consume(event)
            await transcript.consume(AgentEvent("message_end", message=message))
            live = state(transcript)
            assert [row[0] for row in live[1]] == expected_kinds
            assert live[0] == "Final answer."
            assert live[1][-1] == ("text", "", "Final answer.", False)
            await transcript.load_history(entries)
            assert state(transcript) == live

    asyncio.run(scenario())


def test_native_search_history_preserves_interleaved_completion_and_final_answer():
    message = AssistantMessage(api="openai-responses")
    events = []

    def emit(kind, index):
        events.append(AgentEvent("message_update", stream_event=StreamEvent(kind, copy.deepcopy(message), index)))

    message.content.append(RawContent("openai-responses", {
        "type": "web_search_call", "id": "ws_0", "status": "in_progress",
    }))
    emit("native_tool_start", 0)
    message.content.append(TextContent("Checking the first source."))
    emit("text_start", 1)
    message.content[0].data.update(status="completed", action={"type": "search", "query": "First source"})
    emit("native_tool_end", 0)
    message.content.append(RawContent("openai-responses", {
        "type": "web_search_call", "id": "ws_2", "status": "in_progress",
    }))
    emit("native_tool_start", 2)
    message.content[2].data.update(status="completed", action={"type": "search", "query": "Second source"})
    emit("native_tool_end", 2)
    message.content.append(TextContent("Final answer."))
    emit("text_start", 3)
    _assert_history_matches_live(events, message, ["tool_call", "text", "tool_result", "tool_call", "tool_result", "text"])


def test_stream_history_retry_keeps_attempts_separate():
    failed = AssistantMessage([TextContent("Partial from the failed attempt.")])
    message = AssistantMessage([TextContent("Final answer.")])
    events = [
        AgentEvent("message_update", stream_event=StreamEvent("text_start", failed, 0)),
        AgentEvent("message_update", stream_event=StreamEvent("retry", failed)),
        AgentEvent("message_update", stream_event=StreamEvent("text_start", message, 0)),
    ]
    _assert_history_matches_live(events, message, ["text", "retry", "text"])
