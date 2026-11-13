"""Fetch tools run through Session permissions, tools, model continuation and storage."""

import json

import httpx
import pytest

from virgo.ai import web
from virgo.ai.types import AssistantMessage, Model, TextContent, ToolCall
from virgo.tools import web_fetch

from tests.support.harness import install_responses, tool_results


@pytest.fixture
def http(monkeypatch):
    state = {"requests": [], "status": 200, "body": "<html><title>论文</title><nav>noise</nav>"
             "<main><h1>Evidence</h1><p>Only the page knows: 42.</p><a href='/details'>More</a>"
             "<script>steal()</script><pre>x = 1\nprint(x)</pre></main></html>", "headers": {"content-type": "text/html"}}

    def handler(request):
        state["requests"].append(request)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(state["status"], json={"title": "论文", "content": "# Evidence\nOnly the page knows: 42."})
        return httpx.Response(state["status"], text=state["body"], headers=state["headers"])

    monkeypatch.setattr(web_fetch, "_client", lambda **kw: httpx.Client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(web_fetch, "_addresses", lambda host, port, ctx, deadline: ["93.184.216.34"])
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return state


def fetch(s, monkeypatch, url="https://example.com/article"):
    seen = install_responses(monkeypatch,
        AssistantMessage(content=[ToolCall("f1", "web_fetch", {"url": url})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Answer")]),
    )
    s.prompt(f"Read {url}")
    return tool_results(s)[-1], seen


@pytest.mark.parametrize("backend", ["http", "deepseek"])
def test_fetch_result_reaches_model_and_persisted_history(make_session, monkeypatch, http, backend):
    s = make_session(persist=True, settings={"compaction": {"enabled": False}})
    if backend == "deepseek":
        s.main.model = Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com")
        s.auth.set("deepseek", {"type": "api_key", "key": "official-test-key"})
    result, seen = fetch(s, monkeypatch)
    assert not result.is_error, result.text()
    data = json.loads(result.text())
    assert data["backend"] == backend and "42" in data["content"]
    assert data["title"] == "论文" and not data["truncated"]
    assert "42" in seen[1][1].messages[-1].text()
    assert any("42" in m.text() for m in s.store.build_context()[0] if hasattr(m, "text"))
    request, = http["requests"]
    if backend == "deepseek":
        assert str(request.url) == "https://api.deepseek.com/v1/tools/fetch"
        assert json.loads(request.content) == {"url": "https://example.com/article"}
        assert request.headers["authorization"] == "Bearer official-test-key"
        assert data["final_url"] is None
    else:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert "authorization" not in request.headers
        assert "steal" not in data["content"] and "noise" not in data["content"]
        assert "https://example.com/details" in data["content"]


@pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 500])
def test_deepseek_errors_are_not_capability_fallbacks(make_session, monkeypatch, http, status):
    s = make_session(settings={"compaction": {"enabled": False}})
    s.main.model = Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com")
    s.auth.set("deepseek", {"type": "api_key", "key": "key"})
    http["status"] = status
    result, _ = fetch(s, monkeypatch)
    assert result.is_error and str(status) in result.text()
    assert len(http["requests"]) == 1


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.com", "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/", "https://example.com/" + "a" * 8192])
def test_unsafe_urls_never_connect(make_session, monkeypatch, http, url):
    result, _ = fetch(make_session(), monkeypatch, url)
    assert result.is_error
    assert not http["requests"]


def test_private_dns_answer_never_connects(make_session, monkeypatch, http):
    monkeypatch.setattr(web_fetch, "_addresses", lambda *args: ["93.184.216.34", "127.0.0.1"])
    result, _ = fetch(make_session(), monkeypatch)
    assert result.is_error and not http["requests"]


def test_exact_url_permission_blocks_before_network(make_session, monkeypatch, http):
    s = make_session(mode="plan", settings={"permissions": {"deny": ["web_fetch(https://example.com/*)"]}})
    result, _ = fetch(s, monkeypatch)
    assert result.is_error and not http["requests"]


def test_redirect_rechecks_permission(make_session, monkeypatch, http):
    s = make_session(mode="plan", settings={"permissions": {"deny": ["web_fetch(https://blocked.example/*)"]}})
    http.update(status=302, headers={"location": "https://blocked.example/private"})
    result, _ = fetch(s, monkeypatch)
    assert result.is_error and len(http["requests"]) == 1


@pytest.mark.parametrize("kind", ["size", "binary", "empty", "loop"])
def test_invalid_page_cannot_be_reported_as_success(make_session, monkeypatch, http, kind):
    s = make_session(settings={"fetch": {"maxBytes": 1024}})
    if kind == "size":
        http["body"] = "x" * 2048
    elif kind == "binary":
        http["headers"] = {"content-type": "application/pdf"}
    elif kind == "empty":
        http["body"] = "<script>nothing</script>"
    else:
        http.update(status=302, headers={"location": "/article"})
    result, _ = fetch(s, monkeypatch)
    assert result.is_error


def test_large_text_is_saved_with_explicit_truncation(make_session, monkeypatch, http):
    http.update(body="Evidence " * 10000, headers={"content-type": "text/plain"})
    s = make_session()
    result, _ = fetch(s, monkeypatch)
    assert not result.is_error
    data = json.loads(result.text())
    assert data["truncated"]
    from pathlib import Path
    assert Path(data["output_path"]).read_text() == http["body"]


def test_quota_applies_across_tool_rounds_and_resets_on_user_request(make_session, monkeypatch, http):
    s = make_session(settings={"fetch": {"maxUses": 1}, "compaction": {"enabled": False}})
    install_responses(monkeypatch,
        AssistantMessage(content=[ToolCall("f1", "web_fetch", {"url": "https://example.com/1"})], stop_reason="tool_use"),
        AssistantMessage(content=[ToolCall("f2", "web_fetch", {"url": "https://example.com/2"})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Done")]),
    )
    s.prompt("Read two pages")
    assert len(http["requests"]) == 1
    assert tool_results(s)[-1].is_error and "maxUses" in tool_results(s)[-1].text()
    result, _ = fetch(s, monkeypatch)
    assert not result.is_error and len(http["requests"]) == 2


def test_proxy_key_is_never_sent_to_fetch_service(make_session, monkeypatch, http):
    s = make_session()
    s.main.model = Model("deepseek", "deepseek-flash", "openai", "https://proxy.example", options={"apiKey": "proxy-secret"})
    result, _ = fetch(s, monkeypatch)
    assert json.loads(result.text())["backend"] == "http"
    assert "authorization" not in http["requests"][0].headers


def test_fetch_key_setting_does_not_reuse_search_key(make_session, monkeypatch, http):
    monkeypatch.setenv("SEARCH_KEY", "search-only-secret")
    monkeypatch.setenv("FETCH_KEY", "fetch-only-secret")
    s = make_session(settings={"search": {"apiKeyEnv": "SEARCH_KEY"}, "fetch": {"mode": "builtin", "backend": "deepseek", "apiKeyEnv": "FETCH_KEY"}})
    result, _ = fetch(s, monkeypatch)
    assert not result.is_error
    assert http["requests"][0].headers["authorization"] == "Bearer fetch-only-secret"


def test_malformed_html_link_does_not_discard_readable_content(make_session, monkeypatch, http):
    http["body"] = '<title>Doc</title><main><p>Evidence 42.</p><a href="https://[invalid">broken</a></main>'
    result, _ = fetch(make_session(), monkeypatch)
    assert not result.is_error
    assert "42" in json.loads(result.text())["content"]


def test_client_execution_uses_the_selected_offer_not_adapter_order(make_session, monkeypatch, http):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "official-key")
    monkeypatch.setattr(web, "ADAPTERS", [lambda model: [
        web.WebOffer("z-fetch", frozenset({"fetch"}), backend="deepseek"),
        web.WebOffer("a-fetch", frozenset({"fetch"}), backend="http"),
    ]])
    result, _ = fetch(make_session(), monkeypatch)
    assert json.loads(result.text())["backend"] == "http"
    assert http["requests"][0].method == "GET"
