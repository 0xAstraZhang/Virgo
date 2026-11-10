"""Hidden scrollbars retain keyboard and mouse navigation."""

from __future__ import annotations

import asyncio

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI


def test_transcript_has_no_scrollbar_gutter_but_still_scrolls(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.transcript
            await transcript.add_notice("\n".join(f"Line {i}" for i in range(100)))
            await pilot.pause()
            assert transcript.max_scroll_y > 0
            assert transcript.scrollbar_size_vertical == 0
            assert transcript.scrollable_content_region.width == transcript.content_region.width
            await pilot.press("ctrl+home")
            await pilot.pause()
            assert transcript.scroll_y == 0
            await pilot.press("pagedown")
            await pilot.pause()
            assert transcript.scroll_y > 0
            assert not transcript.follow_latest
            await pilot.click("#jump")
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.follow_latest
            await pilot.resize_terminal(42, 16)
            await pilot.pause()
            assert transcript.scrollbar_size_vertical == 0
            assert transcript.scrollable_content_region.width == transcript.content_region.width

    asyncio.run(scenario())
