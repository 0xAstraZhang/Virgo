"""A failed or incomplete stream retains useful, portable diagnostic evidence."""

from __future__ import annotations

import json
import threading

import pytest

from virgo import ai
from virgo.ai.providers import faux
from virgo.ai.stream import EventStream, StreamEvent
from virgo.ai.types import AssistantMessage, TextContent, message_from_dict, message_to_dict


def test_stream_exception_keeps_partial_output_and_exception_chain():
    partial = AssistantMessage([TextContent("partial")], api="test", provider="example", model="small")

    def events():
        yield StreamEvent("text_delta", partial, 0, "partial")
        try:
            raise ValueError("underlying failure")
        except ValueError as cause:
            raise RuntimeError("stream failed") from cause

    stream = EventStream(events(), AssistantMessage())
    received = list(stream)
    result = stream.result()
    assert received[-1].type == "error" and result.stop_reason == "error"
    assert result.text() == "partial" and result.provider == "example"
    assert partial.stop_reason == "stop"
    assert result.error_details["phase"] == "model.stream"
    assert result.error_details["exception_type"] == "RuntimeError"
    evidence = json.dumps(result.error_details)
    assert "ValueError" in evidence and "underlying failure" in evidence
    assert "test_stream.py" in evidence and "events" in evidence
    assert result.error_details["context"] == {"api": "test", "provider": "example", "model": "small"}


@pytest.mark.parametrize("with_partial", [False, True])
def test_missing_terminal_event_is_error_not_success(with_partial):
    partial = AssistantMessage([TextContent("unfinished")])
    stream = EventStream(iter([StreamEvent("text_delta", partial)] if with_partial else []), partial)
    events = list(stream)
    assert events[-1].type == "error"
    assert stream.result().stop_reason == "error"
    assert stream.result().error_details["code"] == "STREAM_INCOMPLETE"
    assert stream.result().text() == "unfinished"


@pytest.mark.parametrize("reason", ["stop", "aborted", "refusal"])
def test_explicit_terminal_event_preserves_control_outcomes(reason):
    message = AssistantMessage(stop_reason=reason)
    event_type = "done" if reason == "stop" else "error"
    stream = EventStream(iter([StreamEvent(event_type, message)]), AssistantMessage())
    assert list(stream)[-1].type == event_type
    assert stream.result() is message and message.error_details is None


def test_incomplete_cancelled_stream_does_not_become_failure():
    message = AssistantMessage(stop_reason="aborted")
    stream = EventStream(iter([StreamEvent("start", message)]), message)
    assert stream.result().stop_reason == "aborted"
    assert stream.result().error_details is None


def test_explicit_model_error_gets_diagnostics_without_a_python_exception():
    message = AssistantMessage(stop_reason="error", error_kind="retryable", error_message="temporarily busy")
    stream = EventStream(iter([StreamEvent("error", message)]), message)
    result = stream.result()
    assert result.error_kind == "retryable"
    assert result.error_details["message"] == "temporarily busy"
    assert not result.error_details["traceback"]
    assert not result.error_details["cause_chain"]


def test_provider_setup_exception_is_a_structured_error(monkeypatch):
    def broken(*args):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr(faux, "stream", broken)
    result = ai.stream(ai.resolve_model("faux/test"), ai.Context("private system", [])).result()
    assert result.stop_reason == "error"
    assert result.error_details["phase"] == "model.request"
    assert result.error_details["exception_type"] == "RuntimeError"
    assert "private system" not in json.dumps(result.error_details)


def test_provider_setup_cancellation_stays_aborted(monkeypatch):
    signal = threading.Event()

    def cancelled(*args):
        signal.set()
        raise RuntimeError("cancelled setup")

    monkeypatch.setattr(faux, "stream", cancelled)
    result = ai.stream(ai.resolve_model("faux/test"), ai.Context("", []), ai.StreamOptions(signal=signal)).result()
    assert result.stop_reason == "aborted" and result.error_details is None


def test_message_diagnostics_round_trip_and_old_messages_remain_readable():
    original = AssistantMessage(stop_reason="error", error_message="failed", error_details={"error_id": "e1"})
    restored = message_from_dict(json.loads(json.dumps(message_to_dict(original))))
    assert restored == original
    legacy = message_to_dict(original)
    del legacy["error_details"]
    assert message_from_dict(legacy).error_details is None


def test_legacy_search_started_deserializes_into_native_started():
    base = message_to_dict(AssistantMessage(content=[TextContent("ok")]))
    assert "search_started" not in base

    # Pre-refactor sessions persisted search_started=False on every assistant message.
    restored_false = message_from_dict({**base, "search_started": False})
    assert restored_false.native_started is False

    # True search_started is preserved in native_started, even when native_started was absent or False.
    without_native = {k: v for k, v in base.items() if k != "native_started"}
    assert message_from_dict({**without_native, "search_started": True}).native_started is True
    assert message_from_dict({**base, "native_started": False, "search_started": True}).native_started is True
    assert message_from_dict({**base, "native_started": True, "search_started": False}).native_started is True
    assert "search_started" not in message_to_dict(restored_false)


@pytest.fixture(params=["EventStream", "ai.stream"])
def tracked_stream(request, monkeypatch):
    closed = threading.Event()
    message = AssistantMessage()

    def provider():
        try:
            yield StreamEvent("start", message)
            yield StreamEvent("done", message)
        finally:
            closed.set()

    # Keep both wrappers AND the generator alive. Reference counting must never be
    # what runs finally at a terminal event, result(), explicit close, or scope exit.
    generator = provider()
    inner = EventStream(generator, message)
    monkeypatch.setattr(faux, "stream", lambda *_: inner)
    stream = inner if request.param == "EventStream" else ai.stream(ai.resolve_model("faux/test"), ai.Context("", []))
    yield stream, closed
    generator.close()


def test_retained_stream_closes_before_terminal_delivery(tracked_stream):
    stream, closed = tracked_stream
    events = iter(stream)
    assert next(events).type == "start" and not closed.is_set()
    assert next(events).type == "done"
    assert closed.is_set()


def test_retained_stream_result_closes_provider(tracked_stream):
    stream, closed = tracked_stream
    assert stream.result().stop_reason == "stop"
    assert closed.is_set()


def test_explicit_stream_close_reaches_provider(tracked_stream):
    stream, closed = tracked_stream
    events = iter(stream)
    assert next(events).type == "start" and not closed.is_set()
    stream.close()
    assert closed.is_set()


def test_break_from_scoped_stream_closes_provider(tracked_stream):
    stream, closed = tracked_stream
    with stream as active:
        for event in active:
            assert event.type == "start" and not closed.is_set()
            break
    assert closed.is_set()


def test_retained_iterator_stops_after_explicit_close(tracked_stream):
    stream, closed = tracked_stream
    events = iter(stream)
    assert next(events).type == "start"
    stream.close()
    assert closed.is_set() and list(events) == []
