"""Fetch tools run through Session permissions, tools, model continuation and storage."""

import json
from unittest.mock import Mock

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
             "<script>steal()</script><pre>x = 1\nprint(x)</pre></main></html>", "headers": {"content-type": "text/html"},
             "deepseek_handler": None}

    real_client = httpx.Client

    def handler(request):
        state["requests"].append(request)
        if request.url.host == "api.deepseek.com":
            if state["deepseek_handler"] is not None:
                return state["deepseek_handler"](request)
            payload = json.dumps({"title": "论文", "content": "# Evidence\nOnly the page knows: 42."}).encode()
            return httpx.Response(state["status"], stream=httpx.ByteStream(payload),
                                  headers={"content-type": "application/json"})
        return httpx.Response(state["status"], stream=httpx.ByteStream(state["body"].encode()),
                              headers=state["headers"])

    monkeypatch.setattr(web_fetch.httpx, "Client",
                        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
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


@pytest.mark.parametrize("status", [401, 402, 403])
def test_deepseek_errors_are_not_capability_fallbacks(make_session, monkeypatch, http, status):
    s = make_session(settings={"compaction": {"enabled": False}})
    s.main.model = Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com")
    s.auth.set("deepseek", {"type": "api_key", "key": "key"})
    http["status"] = status
    result, _ = fetch(s, monkeypatch)
    assert result.is_error and str(status) in result.text()
    assert len(http["requests"]) == 1


@pytest.mark.parametrize("failure", [404, 429, 500, "invalid", "timeout"])
def test_deepseek_auto_retrieval_failure_falls_back_to_http(make_session, monkeypatch, http, failure):
    s = make_session(settings={"compaction": {"enabled": False}})
    s.main.model = Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com")
    s.auth.set("deepseek", {"type": "api_key", "key": "key"})

    def deepseek_fail(_request):
        if failure == "timeout":
            raise httpx.ReadTimeout("deepseek reader timed out")
        if failure == "invalid":
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"),
                                  headers={"content-type": "application/json"})
        return httpx.Response(failure, stream=httpx.ByteStream(b"upstream error"),
                              headers={"content-type": "text/plain"})

    http["deepseek_handler"] = deepseek_fail
    result, _ = fetch(s, monkeypatch, "https://arxiv.org/search/?query=KV+cache&order=-announced_date_first")
    assert not result.is_error, result.text()
    data = json.loads(result.text())
    assert data["backend"] == "http"
    assert "42" in data["content"]
    assert [r.url.host for r in http["requests"]] == ["api.deepseek.com", "93.184.216.34"]


def test_deepseek_explicit_builtin_does_not_fallback_to_http(make_session, monkeypatch, http):
    s = make_session(settings={"compaction": {"enabled": False},
                               "fetch": {"mode": "builtin", "backend": "deepseek"}})
    s.main.model = Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com")
    s.auth.set("deepseek", {"type": "api_key", "key": "key"})
    http["status"] = 404
    result, _ = fetch(s, monkeypatch)
    assert result.is_error and "404" in result.text()
    assert len(http["requests"]) == 1


@pytest.mark.parametrize(("media_type", "body"), [
    ("application/atom+xml", "<feed><title>arXiv Query</title></feed>"),
    ("application/xml", "<OAI-PMH><record>ok</record></OAI-PMH>"),
    ("text/xml", "<root><item>42</item></root>"),
    ("application/json", '{"papers": [{"id": "2611.31105"}]}'),
    ("application/ld+json", '{"@type": "ScholarlyArticle"}'),
])
def test_http_fetch_accepts_structured_text_types(make_session, monkeypatch, http, media_type, body):
    http.update(body=body, headers={"content-type": f"{media_type}; charset=utf-8"})
    result, _ = fetch(make_session(), monkeypatch, "https://export.arxiv.org/api/query?search_query=all:kv")
    assert not result.is_error, result.text()
    data = json.loads(result.text())
    assert data["backend"] == "http"
    assert data["content"] == body


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.com", "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/", "https://example.com/" + "a" * 8192])
def test_unsafe_urls_never_connect(make_session, monkeypatch, http, url):
    result, _ = fetch(make_session(), monkeypatch, url)
    assert result.is_error
    assert not http["requests"]


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/secret", "http://10.0.0.1/", "http://192.168.1.1/",
    "http://[::1]/", "http://198.18.2.29/", "http://localhost/admin",
    "http://app.localhost/", "http://app.localhost./",
])
def test_private_ip_or_localhost_rejected_before_dns(make_session, monkeypatch, http, language, url):
    resolve = Mock(return_value=[])
    monkeypatch.setattr(web_fetch, "_addresses", resolve)
    session = make_session(settings={"language": language})
    result, _ = fetch(session, monkeypatch, url)
    resolve.assert_not_called()
    assert not http["requests"]
    assert result.is_error and result.text() == session.t("tools.fetch.public_only")


@pytest.mark.parametrize(("language", "description"), [("en", "non-public address"), ("zh-CN", "非公网地址")])
@pytest.mark.parametrize("addresses", [["198.18.2.29"], ["10.0.0.5"], ["93.184.216.34", "127.0.0.1"]])
def test_private_dns_answer_never_connects(make_session, monkeypatch, http, language, description, addresses):
    monkeypatch.setattr(web_fetch, "_addresses", lambda *args: addresses)
    session = make_session(settings={"language": language})
    result, _ = fetch(session, monkeypatch)
    assert result.is_error and not http["requests"]
    message = result.text()
    assert all(address not in message for address in addresses)
    assert description in message and "DNS" in message and "/fetch deepseek" in message
    assert message == session.t("tools.fetch.dns_nonpublic")


def test_redirect_to_hostname_with_private_dns_answer_stops_at_first_request(make_session, monkeypatch, http):
    addresses = {"example.com": ["93.184.216.34"], "redirect.example": ["10.0.0.5"]}
    monkeypatch.setattr(web_fetch, "_addresses", lambda host, *_: addresses[host])
    http.update(status=302, headers={"location": "https://redirect.example/page"})
    session = make_session()
    result, _ = fetch(session, monkeypatch)
    assert [request.headers["host"] for request in http["requests"]] == ["example.com"]
    assert result.is_error and result.text() == session.t("tools.fetch.dns_nonpublic")
    assert "10.0.0.5" not in result.text()


def test_read_only_web_fetch_needs_no_grant_and_keeps_shell_network_off(broker_session, http):
    """An ordinary web read needs zero grants and leaves Shell network disabled."""
    from virgo.ai.providers import faux

    args = {"url": "https://example.com/article"}
    session = broker_session(faux.reply(tools=[("web_fetch", args)]), faux.reply(tools=[("web_fetch", args)]),
                             faux.reply("done"), preset="read-only")
    session.prompt("Read the selected article twice")
    results = tool_results(session)
    assert len(results) == 2 and all(not result.is_error for result in results)
    assert len(http["requests"]) == 2
    assert not session.store.custom("permission_request") and not session.ui.questions
    assert not session.main.permission_snapshot.mode.network
    assert not session.main.execution_context.resources.network


def test_redirect_rechecks_permission(make_session, monkeypatch, http):
    s = make_session()
    from virgo.agent.types import Block
    s.bus.on("tool_call", lambda event, _: Block("blocked redirect") if event.args.get("url", "").startswith("https://blocked.example/") else None)
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
    record = s.main.execution_context.outputs.lookup(data["output_id"])
    assert record._path.read_text() == http["body"]



def test_read_only_web_spill_uses_private_workspace_output(broker_session, monkeypatch, http):
    http.update(body="Evidence " * 10000, headers={"content-type": "text/plain"})
    session = broker_session(preset="read-only")
    before = list(session.cwd.iterdir())
    result, _ = fetch(session, monkeypatch)
    assert not result.is_error, result.text()
    output_id = json.loads(result.text())["output_id"]
    record = session.main.execution_context.outputs.lookup(output_id)
    assert record._path.is_relative_to(session.paths.user)
    assert record._path.read_text() == http["body"]
    assert list(session.cwd.iterdir()) == before
    assert not session.ui.questions


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
    result, _ = fetch(make_session(refresh_models=False), monkeypatch)
    assert json.loads(result.text())["backend"] == "http"
    assert http["requests"][0].method == "GET"


def test_shared_web_budget_spans_search_and_fetch_and_resets_on_user_request(make_session, monkeypatch, http):
    """A shared cap must cover both client tools and cannot be bypassed by a hosted offer."""
    from virgo.tools import web as search_http

    requests = []

    def search_page(*args, **kwargs):
        requests.append(args[0])
        return '<a class="result__a" href="https://example.com/article">Evidence</a>'

    monkeypatch.setattr(search_http, "_fetch", search_page)
    session = make_session(settings={"plugins": {"web": {"maxCalls": 1}},
                                     "search": {"mode": "builtin"}, "fetch": {"mode": "builtin"},
                                     "compaction": {"enabled": False}})
    install_responses(monkeypatch,
        AssistantMessage(content=[ToolCall("s1", "web_search", {"query": "evidence"})], stop_reason="tool_use"),
        AssistantMessage(content=[ToolCall("f1", "web_fetch", {"url": "https://example.com/article"})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Budget reached")]),
    )
    session.prompt("Search and read the page")
    assert len(requests) == 1 and not http["requests"]
    assert not tool_results(session)[0].is_error
    assert tool_results(session)[1].is_error and "maxCalls" in tool_results(session)[1].text()
    result, _ = fetch(session, monkeypatch)
    assert not result.is_error and len(http["requests"]) == 1
    session.settings.set("search.mode", "auto")
    session.settings.set("fetch.mode", "auto")
    session.main.model = Model("anthropic", "claude-sonnet-4-6", "anthropic")
    assert not session.main.request_context(session.main.messages).web_tools


@pytest.mark.parametrize(("backend", "status", "expected"), [
    ("deepseek", 404, "target page status is unknown"),
    ("http", 429, "rate-limited"),
])
def test_fetch_failure_identifies_evidence_in_next_model_request(make_session, monkeypatch, http, backend, status, expected):
    session = make_session(settings={"fetch": {"mode": "builtin", "backend": backend}})
    if backend == "deepseek":
        session.auth.set("deepseek", {"type": "api_key", "key": "official-test-key"})
        session.main.model = Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com")
    http["status"] = status
    result, seen = fetch(session, monkeypatch)
    assert result.is_error
    assert expected in seen[1][1].messages[-1].text()
    assert len(http["requests"]) == 1


def test_timeout_distinguishes_incomplete_page_from_http_status(make_session, monkeypatch, http):
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("private request context")

    monkeypatch.setattr(web_fetch, "_http", timeout)
    result, seen = fetch(make_session(), monkeypatch)
    evidence = seen[1][1].messages[-1].text()
    assert result.is_error and "timed out before a complete page" in evidence
    assert "private request context" not in evidence
