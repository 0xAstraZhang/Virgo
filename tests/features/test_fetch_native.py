"""Native fetch through Session, actual Anthropic SDK, HTTP/SSE and history conversion."""

import json

import pytest

from virgo.ai import complete
from virgo.ai.transform import normalize
from virgo.ai.types import Model, RawContent
from virgo.ai.web import native_event

from tests.support.anthropic import ANTHROPIC_EVENTS
from tests.support.http import bypass_proxies, fake_api, sse

URL = "https://example.com/paper"
DOCUMENT = {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_fetch", "content": {
    "type": "web_fetch_result", "url": URL, "retrieved_at": "2026-11-13T00:00:00Z",
    "content": {"type": "document", "title": "Evidence", "source": {
        "type": "text", "media_type": "text/plain", "data": "The measured result is 42."},
        "citations": {"enabled": True}},
}}


def wire(result=DOCUMENT):
    return [
        ANTHROPIC_EVENTS[0],
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "server_tool_use", "id": "srvtoolu_fetch", "name": "web_fetch", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": json.dumps({"url": URL})}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": result},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": "42.", "citations": [{
            "type": "char_location", "document_index": 0, "document_title": "Evidence",
            "start_char_index": 0, "end_char_index": 30, "cited_text": "The measured result is 42."}]}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]


def configure(s, url):
    s.main.model = Model("test-anthropic", "claude-sonnet-4-6", "anthropic", url,
                         options={"apiKey": "fake-key", "nativeFetch": True})


def test_native_fetch_roundtrip_and_provider_switch(make_session, monkeypatch):
    bypass_proxies(monkeypatch)
    with fake_api(body=sse(wire(), named=True)) as server:
        s = make_session(persist=True, settings={"search": {"mode": "off"}, "compaction": {"enabled": False}})
        configure(s, server["url"])
        s.prompt("Read " + URL)
        msg = s.main.messages[-1]
        assert msg.stop_reason == "stop", msg.error_message
        assert msg.native_started
        assert URL in msg.text()
        assert not msg.tool_calls()
        assert {t.get("name") for t in server["requests"][0]["json"]["tools"]} >= {"web_fetch"}
        native = [ev for ev in s.ui.events if ev.stream_event and ev.stream_event.type.startswith("native_tool_")]
        assert len(native) >= 5
        s.prompt("Continue")
        replay = server["requests"][1]["json"]["messages"]
        assert any(block == DOCUMENT for m in replay for block in m["content"])
        foreign = normalize([msg], Model("other", "other", "openai"))
        assert not any(isinstance(b, RawContent) for m in foreign for b in m.content)
        assert URL in foreign[0].text() and "The measured result is 42." in foreign[0].text()
        assert any("web_fetch_tool_result" in json.dumps(entry) for entry in s.store.branch())


def test_fetch_interruption_keeps_evidence_without_retry(make_session, monkeypatch):
    bypass_proxies(monkeypatch)
    with fake_api(body=sse(wire()[:6], named=True)) as server:
        s = make_session(settings={"search": {"mode": "off"}, "compaction": {"enabled": False}})
        configure(s, server["url"])
        s.prompt("Read " + URL)
        msg = s.main.messages[-1]
        assert msg.stop_reason == "error" and msg.native_started
        assert len(server["requests"]) == 1
        assert URL in msg.text() and "partial" in msg.text().lower()


@pytest.mark.parametrize("status,description,kind", [
    (400, "web_fetch_20250910 is not supported", "fetch_unsupported"),
    (404, "page not found", "fatal"),
    (401, "invalid credentials", "fatal"),
])
def test_fetch_capability_error_is_distinct(make_session, monkeypatch, status, description, kind):
    bypass_proxies(monkeypatch)
    with fake_api(status=status, body=json.dumps({"error": {"type": "invalid_request_error", "message": description}}).encode()) as server:
        s = make_session(settings={"search": {"mode": "off"}, "fetch": {"mode": "native"}, "compaction": {"enabled": False}})
        configure(s, server["url"])
        s.prompt("Read " + URL)
        msg = s.main.messages[-1]
        assert msg.error_kind == kind
        assert len(server["requests"]) == 1


def test_auxiliary_completion_never_enables_fetch(make_session, monkeypatch):
    bypass_proxies(monkeypatch)
    with fake_api(body=sse(ANTHROPIC_EVENTS, named=True)) as server:
        s = make_session()
        configure(s, server["url"])
        complete(s.main.model, "Summarize", "A summary")
        assert not server["requests"][0]["json"].get("tools")


@pytest.mark.parametrize("kind", ["pdf", "error"])
def test_native_pdf_and_error_preserve_protocol_without_fake_plaintext(make_session, monkeypatch, kind):
    import copy

    result = copy.deepcopy(DOCUMENT)
    if kind == "pdf":
        result["content"]["content"]["source"] = {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="}
    else:
        result["content"] = {"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"}
    bypass_proxies(monkeypatch)
    with fake_api(body=sse(wire(result), named=True)) as server:
        s = make_session(settings={"search": {"mode": "off"}, "compaction": {"enabled": False}})
        configure(s, server["url"])
        s.prompt("Read " + URL)
        msg = s.main.messages[-1]
        assert msg.content[1].data == result
        assert msg.native_started and msg.stop_reason == "stop"
        info = native_event(result)
        assert info["status"] == ("completed" if kind == "pdf" else "failed")
        foreign = normalize([msg], Model("other", "other", "openai"))
        assert "JVBERi0xLjQ=" not in json.dumps(info) + foreign[0].text()
        if kind == "pdf":
            assert "not portable" in foreign[0].text()
