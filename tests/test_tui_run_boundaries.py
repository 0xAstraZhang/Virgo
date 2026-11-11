"""Run and call boundaries keep earlier answers and repeated tool IDs intact."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from virgo.agent.types import AgentEvent
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_to_dict,
)
from virgo.cli.tui.transcript import AssistantView, ToolView, Transcript


class BoundaryApp(App):
    def compose(self) -> ComposeResult:
        yield Transcript()


def saved(message) -> dict:
    return {"type": "message", "message": message_to_dict(message)}


def boundary(kind: str) -> dict:
    return {"type": "custom", "kind": f"transcript_run_{kind}", "data": {"agent": "main"}}


async def tool_pair(view: Transcript, number: int) -> None:
    await view.consume(AgentEvent("message_end", message=AssistantMessage([
        ToolCall("reused-id", "bash", {"command": f"echo {number}"}),
    ], stop_reason="tool_use")))
    await view.consume(AgentEvent("message_end", message=ToolResultMessage(
        "reused-id", "bash", [TextContent(f"result {number}")],
    )))


def test_history_run_markers_keep_notification_wake_and_previous_final_separate():
    async def scenario():
        async with BoundaryApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            await view.load_history([
                boundary("start"), saved(UserMessage("First request")),
                saved(AssistantMessage([TextContent("First answer")])), boundary("end"),
                boundary("start"), saved(UserMessage("Job completed", kind="notification")),
                saved(AssistantMessage([TextContent("Notification answer")])), boundary("end"),
            ])
            await pilot.pause()
            runs = list(view.query(AssistantView))
            assert [run.text for run in runs] == ["First answer", "Notification answer"]
            assert all(run.body.display for run in runs)
            assert not any(event.kind == "notification" for event in runs[0].events)
            assert any(event.kind == "notification" for event in runs[1].events)

    asyncio.run(scenario())


def test_repeated_call_ids_within_one_run_preserve_both_calls_and_results():
    async def scenario():
        async with BoundaryApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            await view.consume(AgentEvent("agent_start"))
            await tool_pair(view, 1)
            await tool_pair(view, 2)
            await pilot.pause()
            run = view.query_one(AssistantView)
            rows = [event for event in run.events if isinstance(event, ToolView)]
            assert [row.kind for row in rows] == ["tool_call", "tool_result", "tool_call", "tool_result"]
            assert rows[0].args == {"command": "echo 1"}
            assert rows[1].result == "result 1"
            assert rows[2].args == {"command": "echo 2"}
            assert rows[3].result == "result 2"

    asyncio.run(scenario())


def test_repeated_call_ids_across_runs_preserve_previous_rows():
    async def scenario():
        async with BoundaryApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            for number in (1, 2):
                await view.consume(AgentEvent("agent_start"))
                await tool_pair(view, number)
                await view.consume(AgentEvent("message_end", message=AssistantMessage([
                    TextContent(f"Answer {number}"),
                ])))
                await view.consume(AgentEvent("agent_end"))
            await pilot.pause()
            runs = list(view.query(AssistantView))
            assert [run.text for run in runs] == ["Answer 1", "Answer 2"]
            for number, run in enumerate(runs, 1):
                rows = [event for event in run.events if isinstance(event, ToolView)]
                assert len(rows) == 2
                assert rows[0].args == {"command": f"echo {number}"}
                assert rows[1].result == f"result {number}"

    asyncio.run(scenario())


def test_legacy_notification_after_a_final_answer_starts_another_run():
    async def scenario():
        async with BoundaryApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            await view.load_history([
                saved(UserMessage("First request")),
                saved(AssistantMessage([TextContent("First answer")])),
                saved(UserMessage("Job completed", kind="notification")),
                saved(AssistantMessage([TextContent("Notification answer")])),
            ])
            await pilot.pause()
            runs = list(view.query(AssistantView))
            assert [run.text for run in runs] == ["First answer", "Notification answer"]
            assert all(run.body.display for run in runs)

    asyncio.run(scenario())
