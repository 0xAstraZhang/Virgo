"""Helpers for Textual Pilot tests of the fullscreen frontend."""

from __future__ import annotations

import time

from textual.app import App, ComposeResult

from virgo.ai.types import message_to_dict
from virgo.cli.tui.transcript import AssistantView, Transcript

from tests.support.harness import wait_until


async def wait_for(pilot, predicate, *, timeout: float = 4) -> None:
    """Let the app process events until ``predicate`` holds; fail the test if it never does."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "UI did not reach the expected state"
        await pilot.pause(0.01)


def pump(controller, predicate=lambda c: not c.busy) -> list:
    """Poll a RunController (as the app's timer would) until ``predicate``; return the updates."""
    updates = []

    def done():
        updates.extend(controller.poll())
        return predicate(controller)

    wait_until(done, timeout=3)
    return updates


def has_reply(app, text) -> bool:
    return any(
        card.text == text and card.body._markdown == text for card in app.transcript.query(AssistantView)
    )


def form_is_ready(app, question=None) -> bool:
    request = app.input_request
    return (
        request is not None
        and (question is None or request.question == question)
        # State changes precede Textual's layout pass; geometry assertions need
        # the newly displayed form to have reached the screen as well.
        and app.query_one("#auth-question").region.height > 0
    )


class TranscriptApp(App):
    """A bare app around one Transcript, for rendering tests without a session."""

    def compose(self) -> ComposeResult:
        yield Transcript(id="transcript")


def saved_entry(message) -> dict:
    """A message entry as the session store writes it."""
    return {"type": "message", "message": message_to_dict(message)}
