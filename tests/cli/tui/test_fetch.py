"""The actual Textual transcript and picker show reading distinctly from search."""

import asyncio

from virgo.agent.types import AgentEvent
from virgo.ai.types import AssistantMessage, RawContent, TextContent
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.transcript import ToolView, Transcript

from tests.support.tui import TranscriptApp, saved_entry, wait_for


def test_native_open_page_and_find_are_reading_events_live_and_replayed():
    message = AssistantMessage(content=[
        RawContent("openai-responses", {"type": "web_search_call", "id": "open", "status": "completed",
            "action": {"type": "open_page", "url": "https://example.com"}}),
        RawContent("openai-responses", {"type": "web_search_call", "id": "find", "status": "completed",
            "action": {"type": "find_in_page", "url": "https://example.com", "pattern": "evidence"}}),
        TextContent("Read the page."),
    ])

    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(90, 30)) as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("message_start", message=message))
            await view.consume(AgentEvent("message_end", message=message))
            for replay in (False, True):
                if replay:
                    await view.load_history([saved_entry(message)])
                await pilot.pause()
                rows = list(view.query(ToolView))
                assert [row.tool_name for row in rows] == ["web_fetch", "web_fetch", "web_find", "web_find"]
                assert all("https://example.com" in row.text for row in rows)
    asyncio.run(scenario())


def test_fetch_picker_changes_backend_without_chat_message(make_session):
    async def scenario():
        ui = FullscreenUI()
        s = make_session(ui=ui)
        app = VirgoApp(s, ui)
        async with app.run_test(size=(90, 26)) as pilot:
            app.controller.submit("/fetch")
            await wait_for(pilot, lambda: app.input_request is not None)
            await pilot.press("down", "enter")
            await wait_for(pilot, lambda: not app.controller.busy and app.input_request is None)
            assert s.services["fetch"].selection == "http"
            assert not s.main.messages
    asyncio.run(scenario())
