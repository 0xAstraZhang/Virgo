"""Assistant message event stream.

A provider implements a generator of `StreamEvent`s ending with a `done` or `error` event.
`EventStream` wraps it with the Pi contract: iterating never raises, and failures become an
`error` event whose message has `stop_reason="error"`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass

from ..diagnostics import capture_error, safe_text
from .types import AssistantMessage

EVENT_TYPES = (
    "start",
    "text_start",
    "text_delta",
    "text_end",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "toolcall_start",
    "toolcall_delta",
    "toolcall_end",
    # Provider-hosted tools remain RawContent and never enter the local tool loop.
    "native_tool_start",
    "native_tool_delta",
    "native_tool_end",
    "retry",  # harness-level: the attempt failed and is being retried; discard partial output
    "done",
    "error",
)


@dataclass
class StreamEvent:
    type: str
    partial: AssistantMessage
    index: int = -1
    delta: str = ""


class EventStream:
    def __init__(self, events: Iterator[StreamEvent], fallback: AssistantMessage):
        self._events = events
        self._final: AssistantMessage | None = None
        self._fallback = fallback
        self._consumed = False

    def __iter__(self) -> Iterator[StreamEvent]:
        if self._consumed:
            return
        self._consumed = True
        last = self._fallback
        try:
            for ev in self._events:
                last = ev.partial
                if ev.type in ("done", "error"):
                    if last.stop_reason == "error" and last.error_details is None:
                        last.error_details = capture_error(
                            last.error_message or "The model request failed.", phase="model.stream",
                            kind=last.error_kind or "fatal", context=_context(last),
                        )
                        last.error_message = safe_text(last.error_message or last.error_details["message"])
                    self._final = ev.partial
                    yield ev
                    return
                yield ev
            # EOF is not evidence of success: retain partial output and the missing terminal event.
            msg = copy.copy(last)
            if msg.stop_reason != "aborted":
                msg.stop_reason = "error"
                msg.error_kind = "fatal"
                msg.error_message = "The model stream ended without a terminal event."
                msg.error_details = capture_error(
                    msg.error_message, phase="model.stream", kind=msg.error_kind,
                    context=_context(msg), code="STREAM_INCOMPLETE",
                )
            self._final = msg
            yield StreamEvent("error", msg)
        except Exception as e:  # noqa: BLE001 - contract: never raise
            msg = copy.copy(last)
            if msg.stop_reason != "aborted":
                msg.stop_reason = "error"
                msg.error_kind = msg.error_kind or "fatal"
                msg.error_details = capture_error(e, phase="model.stream", kind=msg.error_kind, context=_context(msg))
                msg.error_message = safe_text(f"{type(e).__name__}: {msg.error_details['message']}")
            self._final = msg
            yield StreamEvent("error", msg)

    def result(self) -> AssistantMessage:
        for _ in self:
            pass
        return self._final or self._fallback


def error_message(api: str, provider: str, model: str, text: str, kind: str = "fatal") -> AssistantMessage:
    return AssistantMessage(
        api=api, provider=provider, model=model, stop_reason="error", error_message=safe_text(text), error_kind=kind,
        error_details=capture_error(text, phase="model.request", kind=kind,
                                    context={"api": api, "provider": provider, "model": model}),
    )


def _context(message: AssistantMessage) -> dict:
    return {"api": message.api, "provider": message.provider, "model": message.model}
