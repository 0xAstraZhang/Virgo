"""The Codex account transport and SSE parser against a local server."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from virgo.ai.providers import codex, faux
from virgo.ai.providers._http import ProviderError, _parse_events
from virgo.ai.transform import normalize
from virgo.ai.types import Context, Model, StreamOptions, ToolSpec, UserMessage

from tests.support.harness import RecordingUI
from tests.support.http import sse


@pytest.fixture
def account_server(server):
    server["content_type"] = "text/event-stream"
    return server


def _model(server):
    return Model(
        provider="openai-codex", id="gpt-5-codex", api="openai-codex", base_url=server["url"],
        reasoning=True,
        options={"apiKey": "private-test-token", "authType": "oauth", "authMetadata": {"accountId": "acct-1"}},
    )


def test_codex_account_stream_and_request(account_server):
    text = {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "Reading."}]}
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": '{"path":"a"}'}
    account_server["body"] = sse([
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Reading."},
        {"type": "response.output_item.done", "output_index": 0, "item": text},
        {"type": "response.output_item.added", "output_index": 1, "item": {**call, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"path":"a"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": call},
        {"type": "response.completed", "response": {
            "status": "completed", "output": [text, call],
            "usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 60}},
        }},
    ], done=False)
    model = _model(account_server)
    ctx = Context("SYS", [UserMessage("read a")], [ToolSpec("read", "Read", {"type": "object"})])
    events = list(codex.stream(model, ctx, StreamOptions(effort="high")))
    result = events[-1].partial
    assert events[-1].type == "done"
    assert result.stop_reason == "tool_use" and result.text() == "Reading."
    assert result.tool_calls()[0].arguments == {"path": "a"}
    assert (result.usage.input, result.usage.cache_read, result.usage.output) == (40, 60, 20)
    request = account_server["requests"][0]
    assert request["path"] == "/codex/responses"
    assert request["headers"]["authorization"] == "Bearer private-test-token"
    assert request["headers"]["chatgpt-account-id"] == "acct-1"
    assert request["json"]["instructions"] == "SYS"
    assert request["json"]["reasoning"]["effort"] == "high"
    assert request["json"]["store"] is False and request["json"]["stream"] is True
    assert "max_output_tokens" not in request["json"]
    assert request["json"]["tools"][0]["name"] == "read"


def test_codex_read_tool_output(account_server, responses_read):
    history, vision, expected = responses_read
    model = _model(account_server)
    model.id, model.vision = "reader", vision
    history[1].api = model.api
    before = copy.deepcopy(history)
    ctx = Context("", normalize(history, model))
    account_server["body"] = sse([{"type": "response.completed", "response": {"status": "completed", "output": []}}])
    assert codex.stream(model, ctx, StreamOptions()).result().stop_reason == "stop"
    items = account_server["requests"][0]["json"]["input"]
    assert (items[1]["call_id"], items[1]["id"]) == ("call_1", "fc_1")
    assert items[2:] == [expected]
    assert history == before


def test_codex_workspace_edit_approved_read_outputs(broker_session, project, pixel_png):
    text = project.parent / "outside-approved.txt"
    content = "approved external text\n"
    text.write_bytes(content.encode())
    image = (project / "pixel.png").rename(project.parent / "pixel.png")
    ui = RecordingUI(["y", "y"])
    session = broker_session(
        faux.reply(tools=[("read", {"path": str(text)}, "call_text")]),
        faux.reply(tools=[("read", {"path": str(image)}, "call_image")]),
        faux.reply("done"), preset="workspace-edit", ui=ui,
    )
    session.prompt("Read the external text and image")
    assert len(ui.questions) == 2

    model = Model("openai-codex", "reader", "openai-codex")
    ctx = session.main.request_context(normalize(session.main.messages, model), model)
    params = codex.build_params(model, ctx, StreamOptions())
    assert [item for item in params["input"] if item.get("type") == "function_call_output"] == [
        {"type": "function_call_output", "call_id": "call_text", "output": content},
        {"type": "function_call_output", "call_id": "call_image", "output": [
            {"type": "input_text", "text": "Image: pixel.png"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{pixel_png}", "detail": "auto"},
        ]},
    ]
    boundary = params["instructions"].split("<execution_boundary>")[1].split("\n", 1)[0]
    facts = json.loads(boundary)
    assert "effective" not in facts and "requested" not in facts
    assert facts["mode_defaults"]["read_paths"] == [str(project.resolve())]


@pytest.mark.parametrize("effort", ["xhigh", "max"])
@pytest.mark.parametrize("declared_by", ["supported", "default", "unknown"])
def test_codex_request_preserves_declared_effort_without_low(effort, declared_by):
    model = Model("openai-codex", "test", "openai-codex", reasoning=True)
    if declared_by == "supported":
        model.supported_reasoning_levels = ("high", effort)
    elif declared_by == "default":
        model.default_reasoning_level = effort
    params = codex.build_params(model, Context("", [UserMessage("hi")]), StreamOptions(effort=effort))
    expected = "xhigh" if declared_by == "unknown" and effort == "max" else effort
    assert params["reasoning"]["effort"] == expected


def test_codex_truncated_stream_fails(account_server):
    account_server["body"] = sse([{"type": "response.output_text.delta", "output_index": 0, "delta": "Partial"}], done=False)
    result = codex.stream(_model(account_server), Context("", [UserMessage("hi")]), StreamOptions()).result()
    assert result.stop_reason == "error" and result.error_kind == "retryable"


def test_codex_cancel_does_not_wait_for_stalled_stream(account_server):
    account_server["stall"] = True
    signal = threading.Event()

    def cancel():
        account_server["started"].wait(2)
        signal.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    start = time.monotonic()
    result = codex.stream(_model(account_server), Context("", []), StreamOptions(signal=signal)).result()
    thread.join(1)
    assert result.stop_reason == "aborted"
    assert time.monotonic() - start < 1.5


def test_codex_missing_login_never_sends_request(account_server):
    model = _model(account_server)
    model.options.clear()
    result = codex.stream(model, Context("", []), StreamOptions()).result()
    assert result.stop_reason == "error" and "/login" in result.error_message
    assert account_server["requests"] == []


def test_sse_parser_preserves_multiline_event_and_flushes_at_eof():
    assert list(_parse_events([": keepalive", 'data: {"a":', 'data: 1}'])) == [{"a": 1}]


def test_sse_parser_does_not_echo_bad_response_data():
    with pytest.raises(ProviderError, match="invalid streaming event") as caught:
        list(_parse_events(["data: secret-body", ""]))
    assert "secret-body" not in str(caught.value)
