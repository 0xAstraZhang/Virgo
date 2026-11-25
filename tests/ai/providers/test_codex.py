"""The Codex account transport and SSE parser against a local server."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

from virgo.ai.providers import codex, faux
from virgo.ai.providers._http import AsyncSDKBridge, ProviderError, _parse_events
from virgo.ai.transform import normalize
from virgo.ai.types import Context, Model, StreamOptions, ToolSpec, UserMessage

from tests.support.harness import RecordingUI
from tests.support.http import serve, sse


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
        {"type": "function_call_output", "call_id": "call_text", "output": f"1|{content}"},
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


@pytest.fixture
def bridge():
    with closing(AsyncSDKBridge()) as bridge:
        yield bridge


@pytest.mark.parametrize("shared", [False, True], ids=["request-owned", "session-owned"])
def test_codex_cancel_does_not_wait_for_stalled_stream(account_server, bridge, shared):
    account_server["stall"] = True
    signal = threading.Event()

    def cancel():
        account_server["started"].wait(2)
        signal.set()

    thread = threading.Thread(target=cancel)
    thread.start()
    start = time.monotonic()
    opts = StreamOptions(signal=signal, io_bridge=bridge if shared else None)
    result = codex.stream(_model(account_server), Context("", []), opts).result()
    thread.join(1)
    assert result.stop_reason == "aborted"
    assert time.monotonic() - start < 1.5
    assert account_server["peer_closed"].wait(1), "cancelled request left a live connection"


@pytest.mark.parametrize("shared", [False, True], ids=["request-owned", "session-owned"])
def test_codex_terminal_event_releases_open_response(account_server, monkeypatch, bridge, shared):
    # Record ownership on the real transport while the peer holds the body open.
    owners = []
    closes = []
    init = httpx.Response.__init__
    close = httpx.Response.close
    aclose = httpx.Response.aclose

    def record_init(response, *args, **kwargs):
        init(response, *args, **kwargs)
        owners.append(asyncio.current_task())

    def record_close(response):
        closes.append(asyncio.current_task())
        return close(response)

    async def record_aclose(response):
        closes.append(asyncio.current_task())
        await aclose(response)

    monkeypatch.setattr(httpx.Response, "__init__", record_init)
    monkeypatch.setattr(httpx.Response, "close", record_close)
    monkeypatch.setattr(httpx.Response, "aclose", record_aclose)
    terminal = {"type": "response.completed", "response": {"status": "completed", "output": []}}
    account_server.update(stall=True, stall_at="delta", prefix=sse([terminal], done=False), stall_timeout=10)
    opts = StreamOptions(io_bridge=bridge if shared else None)
    result = codex.stream(_model(account_server), Context("", []), opts).result()
    assert result.stop_reason == "stop"
    assert len(owners) == 1 and set(closes) == set(owners), "response closed outside its I/O owner"
    assert account_server["peer_closed"].wait(1), "terminal response left a live connection"


def test_consumer_closes_stalled_stream_and_can_reuse_bridge(account_server, bridge):
    delta = {"type": "response.output_text.delta", "output_index": 0, "delta": "Partial"}
    account_server.update(stall=True, stall_at="delta", prefix=sse([delta], done=False))
    model = _model(account_server)
    opts = StreamOptions(io_bridge=bridge)
    with codex.stream(model, Context("", []), opts) as events:
        assert any(event.type == "text_delta" for event in events)
    assert account_server["peer_closed"].wait(1)
    account_server.update(stall=False, body=sse([
        {"type": "response.completed", "response": {"status": "completed", "output": []}},
    ]))
    assert codex.stream(model, Context("", []), opts).result().stop_reason == "stop"


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


def test_codex_reuses_connection_and_closes_client_on_bridge_shutdown(monkeypatch):
    peers = []
    clients = []
    init = httpx.AsyncClient.__init__

    def observe(client, **kwargs):
        init(client, **kwargs)
        clients.append(client)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            peers.append(self.client_address)
            body = sse([{"type": "response.completed", "response": {"status": "completed", "output": []}}])
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    monkeypatch.setattr(httpx.AsyncClient, "__init__", observe)
    with serve(Handler) as url, closing(AsyncSDKBridge()) as bridge:
        opts = StreamOptions(prompt_cache_key="one", io_bridge=bridge)
        for _ in range(2):
            assert codex.stream(_model({"url": url}), Context("", []), opts).result().stop_reason == "stop"
        assert len(clients) == 1 and not clients[0].is_closed
        assert len(peers) == 2 and peers[0] == peers[1]
    assert clients[0].is_closed


def test_codex_affinity_is_filtered_scoped_and_expires(monkeypatch):
    seen = []
    clients = []
    reply_headers = []
    base = "https://chat.example.test"
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)

    def respond(request):
        seen.append(request.headers)
        return httpx.Response(200, headers=reply_headers, content=sse([
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        ]))

    init = httpx.AsyncClient.__init__

    def mock_transport(client, **kwargs):
        init(client, transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_transport)
    with closing(AsyncSDKBridge()) as bridge:
        def send(url=base, runtime="one", account="acct-1"):
            model = _model({"url": url})
            model.options["headers"] = {"cOoKiE": "auth=private"}
            model.options["authMetadata"]["accountId"] = account
            opts = StreamOptions(prompt_cache_key=runtime, io_bridge=bridge)
            result = codex.stream(model, Context("", []), opts).result()
            assert result.stop_reason == "stop", result.error_message
            return seen[-1]

        reply_headers.extend([
            ("set-cookie", "__oailb=a; Path=/; Domain=example.test; Max-Age=10"),
            ("set-cookie", "__cflb=b; Path=/codex; Secure"),
            ("set-cookie", "auth=private; Path=/"),
            ("x-codex-turn-state", "first"),
        ])
        first = send()
        assert "cookie" not in first and "x-codex-turn-state" not in first
        assert first["session-id"] == first["thread-id"] == "one"
        assert all(cookie.name in {"__oailb", "__cflb"} for cookie in clients[0].cookies.jar)
        reply_headers.clear()
        for url in ("https://child.chat.example.test", "http://chat.example.test", base + ":8443", base + "/alternate"):
            other = send(url)
            assert "cookie" not in other and "x-codex-turn-state" not in other
        sibling = send(runtime="two")
        assert set(sibling["cookie"].split("; ")) == {"__oailb=a", "__cflb=b"}
        assert "x-codex-turn-state" not in sibling
        other = send(account="acct-2")
        assert "cookie" not in other and "x-codex-turn-state" not in other
        reply_headers.extend([
            ("set-cookie", "__cflb=gone; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/codex"),
            ("set-cookie", "__oailb=" + "x" * 8193 + "; Path=/; Domain=example.test"),
            ("x-codex-turn-state", "second"),
        ])
        again = send()
        assert set(again["cookie"].split("; ")) == {"__oailb=a", "__cflb=b"}
        assert again["x-codex-turn-state"] == "first"
        reply_headers.clear()
        assert send()["cookie"] == "__oailb=a"
        clock += 11
        expired = send()
        assert "cookie" not in expired and expired["x-codex-turn-state"] == "second"
