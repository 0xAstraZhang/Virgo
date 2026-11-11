"""Long saved sessions open with their latest runs; older runs load when scrolled to."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen

from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_to_dict,
)
from virgo.cli.tui import transcript as transcript_module
from virgo.cli.tui.transcript import AssistantView, Transcript


class HistoryApp(App):
    def compose(self) -> ComposeResult:
        yield Transcript()


def saved(message) -> dict:
    return {"type": "message", "message": message_to_dict(message)}


def custom(kind: str, **data) -> dict:
    return {"type": "custom", "kind": kind, "data": {"agent": "main", **data}}


def saved_run(number: int) -> list[dict]:
    thought = ThinkingContent(f"Thought {number}")
    call = ToolCall(f"call-{number}", "read", {"path": f"file_{number}.py"})
    result = ToolResultMessage(call.id, "read", [TextContent(f"contents {number}")], details={"duration": 0.5})
    answer = TextContent(f"Answer {number}")
    return [
        custom("transcript_run_start"),
        saved(UserMessage(f"Prompt {number}")),
        custom("transcript_stream", stream_type="thinking_start", index=0, block=asdict(thought)),
        custom("transcript_stream", stream_type="toolcall_start", index=1, block=asdict(call)),
        saved(AssistantMessage([thought, call], stop_reason="tool_use")),
        custom("transcript_tool_result", tool_call_id=call.id, tool_name="read", args=call.arguments,
               message=message_to_dict(result)),
        saved(result),
        custom("transcript_stream", stream_type="text_start", index=0, block=asdict(answer)),
        saved(AssistantMessage([answer])),
        custom("transcript_run_end"),
    ]


def session(runs: int, prompt: str = "Prompt") -> list[dict]:
    entries = [saved(UserMessage("Legacy prompt")), saved(AssistantMessage([TextContent("Legacy answer")]))]
    for number in range(runs):
        entries += saved_run(number)
    if prompt != "Prompt":
        for entry in entries:
            if entry.get("message", {}).get("role") == "user":
                entry["message"]["content"] = entry["message"]["content"].replace("Prompt", prompt)
    return entries


def prompts(view: Transcript) -> list[str]:
    return [str(child.content) for child in view.query(".user-message")]


def shown(view: Transcript) -> list:
    rows = []
    for child in view.children:
        if isinstance(child, AssistantView):
            rows.append((
                child.text, child.show_thinking, child.thought_body.display, str(child.thought_header.content),
                [(event.kind, event.text, event.expanded, event.display, str(event.header.content))
                 for event in child.events],
            ))
        else:
            rows.append((sorted(child.classes), str(child.content)))
    return rows


async def settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.parametrize("toggle", [None, "tools", "thinking"])
def test_older_runs_load_on_demand_and_match_a_full_replay(monkeypatch, toggle):
    entries = session(12)

    async def open_and_go_home(history_runs: int):
        monkeypatch.setattr(transcript_module, "HISTORY_RUNS", history_runs)
        async with HistoryApp().run_test(size=(80, 24)) as pilot:
            view = pilot.app.query_one(Transcript)
            await view.load_history(entries)
            initially = prompts(view)
            await settle(pilot)
            if toggle == "tools":
                view.toggle_tools()
            elif toggle == "thinking":
                view.toggle_thinking()
            view.action_scroll_home()
            await settle(pilot)
            assert view.scroll_y == 0
            maps = [[(key, row.text, row.status) for key, row in rows.items()] for rows in (view.tools, view.results)]
            return initially, shown(view), maps

    eager = asyncio.run(open_and_go_home(1000))
    lazy = asyncio.run(open_and_go_home(3))
    assert eager[0] == ["> Legacy prompt", *[f"> Prompt {n}" for n in range(12)]]
    assert lazy[0] == ["> Prompt 9", "> Prompt 10", "> Prompt 11"]
    assert lazy[1:] == eager[1:]


def test_prepending_older_runs_never_moves_the_reader(monkeypatch):
    monkeypatch.setattr(transcript_module, "HISTORY_RUNS", 4)

    async def scenario():
        async with HistoryApp().run_test(size=(80, 24)) as pilot:
            view = pilot.app.query_one(Transcript)
            await view.load_history(session(40))
            await settle(pilot)
            assert "> Prompt 0" not in prompts(view)
            reader, frames = view.children[0], []
            original_refresh = Screen._compositor_refresh

            def record_frame(screen: Screen) -> None:
                original_refresh(screen)
                frames.append(reader.region.y)

            monkeypatch.setattr(Screen, "_compositor_refresh", record_frame)
            view.scroll_to(y=0, animate=False, immediate=True)
            await settle(pilot)
            top = view.content_region.y
            assert view.children[0] is not reader
            assert frames and set(frames) == {top}
            assert reader.region.y == top and view.scroll_y > 0

    asyncio.run(scenario())


def test_page_up_while_older_runs_load_is_kept(monkeypatch):
    monkeypatch.setattr(transcript_module, "HISTORY_RUNS", 4)

    async def scenario():
        async with HistoryApp().run_test(size=(80, 24)) as pilot:
            view = pilot.app.query_one(Transcript)
            await view.load_history(session(40))
            await settle(pilot)
            reader, page = view.children[0], view.scrollable_content_region.height
            view._older_pending = True  # Hold the scroll trigger to start the load by hand.
            view.scroll_to(y=page + 3, animate=False, immediate=True)
            await pilot.pause()
            load = asyncio.create_task(view._load_older())
            while not view._prepending:
                await asyncio.sleep(0)
            view.action_page_up()
            await load
            await settle(pilot)
            assert view.children[0] is not reader
            assert view.scroll_y - reader.virtual_region.y == 3

    asyncio.run(scenario())


def test_opening_another_session_cancels_loading_older_runs(monkeypatch):
    monkeypatch.setattr(transcript_module, "HISTORY_RUNS", 4)

    async def scenario():
        async with HistoryApp().run_test(size=(80, 24)) as pilot:
            view = pilot.app.query_one(Transcript)
            await view.load_history(session(40))
            await settle(pilot)
            view.scroll_to(y=0, animate=False, immediate=True)  # Queues older runs.
            await view.load_history(session(2, prompt="Other"))
            await settle(pilot)
            assert prompts(view) == ["> Legacy prompt", "> Other 0", "> Other 1"]
            assert [key for key in view.tools] == [("main", "call-0"), ("main", "call-1")]

    asyncio.run(scenario())


def test_history_replay_is_mounted_and_laid_out_once(monkeypatch):
    monkeypatch.setattr(transcript_module, "HISTORY_RUNS", 1000)
    layouts = []
    original_layout = Screen._refresh_layout

    def count_layout(screen: Screen, *args, **kwargs) -> None:
        layouts.append(kwargs.get("scroll", False))
        original_layout(screen, *args, **kwargs)

    async def scenario():
        async with HistoryApp().run_test(size=(80, 24)) as pilot:
            view = pilot.app.query_one(Transcript)
            await pilot.pause()
            monkeypatch.setattr(Screen, "_refresh_layout", count_layout)
            await view.load_history(session(40))
            during = len(layouts)
            await settle(pilot)
            # Each incremental mount used to lay out the whole transcript again.
            assert during == 0
            assert layouts.count(False) <= 4
            assert len(prompts(view)) == 41

    asyncio.run(scenario())
