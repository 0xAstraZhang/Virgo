"""Scrollbar thumb and fractional edges keep the brand color while scrolling."""

from __future__ import annotations

import asyncio

from rich.color import Color
from textual.geometry import Offset

from virgo.cli.tui.app import PURPLE, VirgoApp
from virgo.cli.tui.bridge import FullscreenUI


def test_scrollbar_keeps_rgb_purple_at_every_position_and_mouse_state(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.transcript
            await transcript.add_notice("\n".join(f"Line {i}" for i in range(100)))
            await pilot.pause()
            assert transcript.max_scroll_y > 0
            scrollbar = transcript.vertical_scrollbar
            purple = Color.parse(PURPLE)
            saw_edge = saw_body = False

            # Visit both ends and fractional thumb positions in both directions.
            for fraction in (0, 0.17, 0.49, 0.83, 1, 0.83, 0.49, 0.17, 0):
                transcript.scroll_to(y=transcript.max_scroll_y * fraction, animate=False)
                await pilot.pause()
                for mouse_over, grabbed in ((False, None), (True, None), (True, Offset(0, 0))):
                    scrollbar.mouse_over = mouse_over
                    scrollbar.grabbed = grabbed
                    renderable = scrollbar.render()
                    options = app.console.options.update(
                        width=scrollbar.size.width, height=scrollbar.size.height
                    )
                    segments = list(app.console.render(renderable, options))
                    thumb = [
                        segment
                        for segment in segments
                        if segment.style and segment.style.meta.get("@mouse.down") == "grab"
                    ]
                    assert thumb
                    for segment in thumb:
                        assert segment.style.color == purple
                        assert not segment.style.bgcolor or segment.style.bgcolor.is_default
                        saw_edge |= bool(segment.text.strip())
                        saw_body |= not segment.text.strip() and bool(segment.style.reverse)
                scrollbar.grabbed = None
            assert saw_edge and saw_body

    asyncio.run(scenario())
