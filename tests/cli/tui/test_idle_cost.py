"""An idle fullscreen frontend neither re-lays out nor walks the transcript each frame."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from rich.text import Text
from textual.css.query import DOMQuery
from textual.widgets import Markdown, Static

from virgo.agent.types import AgentEvent
from virgo.ai.stream import StreamEvent
from virgo.ai.types import AssistantMessage, TextContent
from virgo.cli.tui import transcript as transcript_module
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.transcript import Transcript

from tests.support.tui import TranscriptApp


def test_idle_frames_neither_relayout_nor_walk_the_transcript(broker_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        session = broker_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            app.update_status()  # settle every label once
            updates, walks = [], []
            original_update, original_bool = Static.update, DOMQuery.__bool__
            monkeypatch.setattr(Static, "update", lambda self, *a, **kw: (updates.append(self.id), original_update(self, *a, **kw))[1])
            monkeypatch.setattr(DOMQuery, "__bool__", lambda self: (walks.append(1), original_bool(self))[1])
            for _ in range(20):
                app.last_status = 0  # also force the twice-a-second status refresh
                await app.poll()
            # Each Static.update relayouts every widget, and query truthiness matches every
            # widget: both made an idle long conversation use a full CPU core.
            assert updates == [] and walks == []
            session.services["execution"].select_mode("read-only")
            app.update_status()
            assert "Read-Only" in str(app.screen_stack[0].query_one("#status", Static).content)

    asyncio.run(scenario())


def test_activity_label_refresh_does_not_relayout(make_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.1)
            card = await app.transcript._assistant("main")
            updates = []
            original = Static.update
            monkeypatch.setattr(Static, "update", lambda self, content="", *, layout=True: (
                updates.append((self, layout)), original(self, content, layout=layout))[1])
            app.activity.begin(time.monotonic(), "medium")
            for _ in range(5):
                app.activity.started_at -= 0.12
                await app.poll()
                before = len(updates)
                app._update_status_line()
                app.transcript.show_activity(app.activity.content)
                assert len(updates) == before
            assert {widget for widget, _ in updates} == {card.thought_header}
            assert not any(layout for _, layout in updates)
            assert "Working" in card.thought_header.content.plain

    asyncio.run(scenario())


def test_text_stall_frames_skip_hidden_repaints_and_visible_relayouts(monkeypatch):
    now = 100.0
    monkeypatch.setattr(transcript_module, "time", SimpleNamespace(monotonic=lambda: now))

    async def scenario():
        nonlocal now
        app = TranscriptApp()
        async with app.run_test() as pilot:
            view = app.query_one(Transcript)

            async def send_text(text):
                stream = StreamEvent("text_delta", AssistantMessage([TextContent(text)]), index=0, delta=text[-1])
                await view.consume(AgentEvent("message_update", stream_event=stream))

            await send_text("a")
            header = view.activity_card.thought_header
            with patch.object(header, "update", wraps=header.update) as repaint:
                for text in ("ab", "abc", "abcd"):
                    now += 0.25
                    await send_text(text)
                    view.show_activity(Text(f"{text[-1]} Responding… (0s)"))
                    assert not header.display
                repaint.assert_not_called()

            now += 2
            view.show_activity(Text("✻ Responding… (2s)"))
            assert header.display
            # Settle the visibility transition and the last streamed Markdown update.
            await pilot.pause(0.15)
            with (
                patch.object(header, "update", wraps=header.update) as repaint,
                patch.object(Markdown, "update", autospec=True, side_effect=Markdown.update) as markdown,
                patch.object(app.screen, "_refresh_layout", wraps=app.screen._refresh_layout) as relayout,
            ):
                for frame in ("✽", "✻", "✶"):
                    label = Text(f"{frame} Responding… (2s)")
                    view.show_activity(label)
                    view.show_activity(label)
                    await pilot.pause()
                assert repaint.call_count == 3
                assert all(call.kwargs == {"layout": False} for call in repaint.call_args_list)
                markdown.assert_not_called()
                relayout.assert_not_called()

    asyncio.run(scenario())
