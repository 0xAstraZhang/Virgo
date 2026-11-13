"""Conversation rendering is exercised in Textual's real, headless event loop."""

from __future__ import annotations

import asyncio

import pytest
from rich.text import Text
from textual.widgets import Static

from virgo.agent.types import AgentEvent, ToolResult
from virgo.ai.stream import StreamEvent
from virgo.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_to_dict,
)
from virgo.cli.tui.transcript import AssistantView, ToolView, Transcript

from tests.support.tui import TranscriptApp, wait_for


async def refresh_transcript(view, pilot):
    """Wait for already queued refresh callbacks, then process their updates."""
    refreshed = asyncio.Event()
    view.call_after_refresh(refreshed.set)
    await asyncio.wait_for(refreshed.wait(), 4)
    await pilot.pause()


def test_stream_and_final_share_one_card_with_folded_thinking():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("message_start", message=AssistantMessage()))
            for text in ("Hello", "Hello **世界**", "Hello **世界**\n\n```python\nprint(1)\n```"):
                message = AssistantMessage([ThinkingContent("Check carefully"), TextContent(text)])
                await view.consume(
                    AgentEvent("message_update", stream_event=StreamEvent("text_delta", message))
                )
            await view.consume(AgentEvent("message_end", message=message))
            await pilot.pause()
            cards = list(view.query(AssistantView))
            assert len(cards) == 1
            card = cards[0]
            assert card.text == message.text()
            assert card.body._markdown == message.text()
            assert card.thinking == "Check carefully"
            assert card.thought_body.display
            assert not card.thought_header.display
            view.toggle_thinking()
            assert card.events[0].output.display
            assert card.thought_body.display
            card.events[0].header.focus()
            await pilot.press("enter")
            assert not card.events[0].output.display
            assert card.thought_body.display

    asyncio.run(scenario())


def test_retry_discards_partial_and_final_error_remains_visible():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            failed = AssistantMessage([TextContent("Partial from failed attempt")])
            await view.consume(AgentEvent("message_start", message=failed))
            await view.consume(AgentEvent("message_update", stream_event=StreamEvent("retry", failed)))
            card = view.active["main"]
            assert card.text == ""
            message = AssistantMessage(
                [TextContent("Fresh answer")], stop_reason="error", error_message="offline"
            )
            await view.consume(AgentEvent("message_update", stream_event=StreamEvent("text_delta", message)))
            await view.consume(AgentEvent("message_end", message=message))
            await pilot.pause()
            assert len(view.query(AssistantView)) == 1
            assert card.text == "Fresh answer"
            assert card.body._markdown == "Fresh answer"
            assert card.error.display
            assert "offline" in str(card.error.content)

    asyncio.run(scenario())


def test_failed_stream_with_partial_tool_call_keeps_failure_visible():
    async def scenario():
        for reason in ("error", "aborted", "refusal"):
            app = TranscriptApp()
            async with app.run_test() as pilot:
                view = app.query_one(Transcript)
                message = AssistantMessage(
                    [TextContent("Preparing a search"), ToolCall("partial", "web_search", {})],
                    stop_reason=reason, error_message="Stream disconnected during tool arguments",
                )
                await view.consume(AgentEvent("message_end", message=message))
                await pilot.pause()
                run = view.query_one(AssistantView)
                assert run.error.display
                assert "Stream disconnected" in str(run.error.content)
                assert run.thought_body.display
                assert [event.kind for event in run.events] == ["text", "tool_call"]

    asyncio.run(scenario())


def test_tools_are_keyed_by_agent_and_call_and_preview_is_foldable():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            assistant = AssistantMessage(
                [ToolCall("shared-id", "bash", {"command": "pwd"})], stop_reason="tool_use"
            )
            await view.consume(AgentEvent("message_end", message=assistant))
            for agent in ("main", "explorer"):
                await view.consume(
                    AgentEvent(
                        "tool_execution_start",
                        agent=agent,
                        tool_call_id="shared-id",
                        tool_name="bash",
                        args={"command": "pwd"},
                    )
                )
            result = ToolResult.text("one\ntwo\nthree\nfour\nfive", details={"duration": 0.5})
            await view.consume(
                AgentEvent(
                    "tool_execution_end",
                    tool_call_id="shared-id",
                    tool_name="bash",
                    result=result,
                    is_error=True,
                )
            )
            await view.consume(
                AgentEvent(
                    "message_end",
                    message=ToolResultMessage("shared-id", "bash", result.content, is_error=True),
                )
            )
            await pilot.pause()
            assert len(view.query(ToolView)) == 3
            main = view.results[("main", "shared-id")]
            assert main.status == "error"
            assert not main.output.display
            view.toggle_thinking()
            main.header.focus()
            await pilot.press("enter")
            assert main.expanded
            assert "four" in str(main.output.content)
            assert "[explorer]" in str(view.tools[("explorer", "shared-id")].header.content)
            view.toggle_tools()
            assert all(t.expanded for t in view.tools.values())

    asyncio.run(scenario())


def test_complete_branch_restores_pre_compaction_messages_and_new_resets():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            entries = [
                {"type": "message", "message": message_to_dict(UserMessage("old user text"))},
                {
                    "type": "message",
                    "message": message_to_dict(AssistantMessage([TextContent("old answer")])),
                },
                {"type": "compaction", "summary": "short summary"},
                {"type": "message", "message": message_to_dict(UserMessage("new user text"))},
            ]
            await view.load_history(entries)
            await pilot.pause()
            visible = "\n".join(
                str(w.content) for w in view.query(Static) if not isinstance(w, AssistantView)
            )
            assert "old user text" in visible
            assert "new user text" in visible
            assert "full conversation retained" in visible
            assert list(view.query(AssistantView))[0].text == "old answer"
            await view.load_history([])
            assert not view.children
            assert not view.active
            assert not view.tools

    asyncio.run(scenario())


def test_streaming_preserves_manual_scroll_and_jump_resumes_following():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(Transcript)
            entries = [
                {"type": "message", "message": message_to_dict(UserMessage(f"message {i}"))}
                for i in range(35)
            ]
            await view.load_history(entries)
            await wait_for(pilot, lambda: view.max_scroll_y > view.scrollable_content_region.height > 0)
            assert view.follow_latest
            assert view.scroll_y == view.max_scroll_y
            view.action_page_up()
            await pilot.pause()
            assert not view.follow_latest
            scroll_y = view.scroll_y
            await view.consume(
                AgentEvent("message_end", message=AssistantMessage([TextContent("new reply\n" * 10)]))
            )
            await pilot.pause()
            assert view.scroll_y == scroll_y
            view.go_latest()
            await pilot.pause()
            assert view.follow_latest
            assert view.scroll_y == view.max_scroll_y
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert view.follow_latest
            assert view.scroll_y == view.max_scroll_y

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["page_up", "scroll_home"])
def test_jump_then_manual_scroll_survives_refresh_and_new_messages(action):
    async def scenario():
        app = TranscriptApp()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(Transcript)
            await view.load_history([
                {"type": "message", "message": message_to_dict(UserMessage(f"message {i}"))}
                for i in range(35)
            ])
            await refresh_transcript(view, pilot)
            assert view.max_scroll_y > view.scrollable_content_region.height > 0
            assert view.scroll_y == view.max_scroll_y

            # No yield between these actions: user input must take precedence
            # over any scroll-to-bottom callback left by the earlier jump.
            view.go_latest()
            getattr(view, f"action_{action}")()
            manual_y = 0 if action == "scroll_home" else view.scroll_y
            assert manual_y < view.max_scroll_y
            assert not view.follow_latest
            await refresh_transcript(view, pilot)
            assert view.scroll_y == manual_y
            assert not view.follow_latest

            old_max = view.max_scroll_y
            await view.consume(
                AgentEvent("message_end", message=AssistantMessage([TextContent("new reply\n" * 10)]))
            )
            await refresh_transcript(view, pilot)
            assert view.max_scroll_y > old_max
            assert view.scroll_y == manual_y
            assert not view.follow_latest

            view.go_latest()
            await refresh_transcript(view, pilot)
            assert view.follow_latest
            assert view.scroll_y == view.max_scroll_y
            await pilot.resize_terminal(100, 30)
            await refresh_transcript(view, pilot)
            assert view.follow_latest
            assert view.scroll_y == view.max_scroll_y

    asyncio.run(scenario())


def test_user_start_and_end_do_not_duplicate_text():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            message = UserMessage("你好\nsecond line")
            await view.consume(AgentEvent("message_start", message=message))
            await view.consume(AgentEvent("message_end", message=message))
            await pilot.pause()
            rows = list(view.query(".user-message"))
            assert len(rows) == 1
            assert str(rows[0].content) == "> 你好\nsecond line"
            assert rows[0].styles.background.hex == "#EEEEEE"
            assert rows[0].styles.color.hex == "#000000"

    asyncio.run(scenario())


def test_inline_activity_is_separate_from_thought_folding_and_follows_prompt():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)
            await view.consume(AgentEvent("turn_start"))
            await view.consume(AgentEvent("message_end", message=UserMessage("question")))
            card = view.activity_card
            view.show_activity(Text("✻ Thinking… (3s · thinking with high effort)"))
            assert view.children[0].has_class("user-message")
            assert view.children[1] is card
            assert len(view.query(AssistantView)) == 1
            assert not view.query(".speaker")
            assert str(card.thought_header.content).startswith("✻ Thinking…")
            assert card.thought_header.has_class("active")

            message = AssistantMessage([ThinkingContent("Consider this carefully.")])
            await view.consume(AgentEvent("message_start", message=message))
            assert view.activity_card is card
            assert len(view.query(".thought-header")) == 1
            card.events[0].header.focus()
            await pilot.press("enter")
            assert card.thought_body.display
            assert str(card.thought_header.content).startswith("✻ Thinking…")
            view.show_activity(Text("✽ Thinking… (4s · thinking with high effort)"))
            assert str(card.thought_header.content).startswith("✽ Thinking…")

            await view.consume(AgentEvent("message_end", message=message))
            await view.consume(AgentEvent("turn_start"))
            view.show_activity(Text("✳ Thinking… (5s · thinking with high effort)"))
            assert str(card.thought_header.content).startswith("✳ Thinking…")
            assert card.thought_header.has_class("active")
            assert view.activity_card is card
            assert len(view.query(".thought-header.active")) == 1
            view.finish_activity()
            assert not view.query(".thought-header.active")
            assert not card.thought_header.display

    asyncio.run(scenario())


def test_history_does_not_receive_activity_from_later_local_commands():
    async def scenario():
        app = TranscriptApp()
        async with app.run_test():
            view = app.query_one(Transcript)
            await view.load_history(
                [{"type": "message", "message": message_to_dict(AssistantMessage([TextContent("saved")]))}]
            )
            card = view.query_one(AssistantView)
            assert view.activity_card is None
            view.show_activity(Text("✻ Working… (1s)"))
            assert not card.thought_header.display
            assert card.activity is None

    asyncio.run(scenario())


def test_markdown_code_blocks_keep_native_terminal_colors():
    async def scenario():
        for theme in ("ansi-dark", "ansi-light"):
            app = TranscriptApp(ansi_color=True)
            app.theme = theme
            async with app.run_test(size=(80, 24)) as pilot:
                view = app.query_one(Transcript)
                text = "```python\nclass Thing:\n    def __init__(self):\n        self.value = 'hello'\n```"
                await view.consume(AgentEvent("message_end", message=AssistantMessage([TextContent(text)])))
                await pilot.pause()
                fence = view.query_one("MarkdownFence")
                assert fence.styles.background.ansi == -1 or fence.styles.background.a == 0
                assert fence.styles.color.ansi == -1

    asyncio.run(scenario())
