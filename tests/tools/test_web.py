"""The web_search tool against mocked DuckDuckGo and DeepSeek endpoints."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from virgo.agent.types import ToolContext
from virgo.tools.web import MAX_BYTES, make_web_search

HTML = """<!doctype html><html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=123">Python <b>documentation</b></a>
<a class="result__snippet">Official Python &amp; library documentation.</a>
<a class="result__a" href="https://docs.python.org/3/">Duplicate</a>
<a class="result__snippet">Not a new result.</a>
<a class="result__a" href="javascript:alert(1)">Invalid URL</a>
<a class="result__a" href="https://www.python.org/">Python home</a>
<div class="result__snippet">Language <b>home</b> page.</div>
</body></html>"""


@pytest.fixture
def context(project):
    return ToolContext("search-1", project, threading.Event())


@pytest.fixture
def transport(monkeypatch):
    state = {"requests": [], "status": 200, "body": HTML, "timeout": None}
    original = httpx.Client

    def handler(request):
        state["requests"].append(request)
        if "error" in state:
            raise state["error"]
        if "stream" in state:
            return httpx.Response(state["status"], stream=state["stream"])
        return httpx.Response(state["status"], text=state["body"])

    def client(**kwargs):
        state["timeout"] = kwargs["timeout"]
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("virgo.tools.web.httpx.Client", client)
    return state


def search(context, settings=None, **args):
    return make_web_search(settings or {}).execute({"query": "Python docs", **args}, context)


def test_ddg_http_results_are_deduplicated_and_model_visible(context, transport):
    result = search(context, query="  Python 中文 docs  ")
    assert not result.is_error, result.as_text()
    payload = json.loads(result.as_text())
    assert payload["query"] == "Python 中文 docs"
    assert payload["backend"] == "duckduckgo" and payload["retrieved_at"].endswith("+00:00")
    assert payload["results"] == [
        {"title": "Python documentation", "url": "https://docs.python.org/3/", "snippet": "Official Python & library documentation."},
        {"title": "Python home", "url": "https://www.python.org/", "snippet": "Language home page."},
    ]
    request = transport["requests"][0]
    assert str(request.url).startswith("https://html.duckduckgo.com/html/?")
    assert request.url.params["q"] == "Python 中文 docs"
    assert "authorization" not in request.headers
    assert result.details["count"] == 2


def test_lite_markup_relative_redirect_and_result_limit(context, transport):
    transport["body"] = """
        <a class="result-link" href="/l/?uddg=https%3A%2F%2Fexample.org%2F">First</a>
        <td class="result-snippet">Summary</td>
        <a class="result-link" href="https://example.com/">Second</a>
    """
    result = search(context, limit=1)
    assert json.loads(result.as_text())["results"] == [
        {"title": "First", "url": "https://example.org/", "snippet": "Summary"},
    ]


@pytest.mark.parametrize("query,limit", [("", 5), (" " * 4, 5), ("x" * 2001, 5), ([], 5), ("q", 0), ("q", 11), ("q", True), ("q", 1.5)])
def test_invalid_inputs_never_send_requests(context, transport, query, limit):
    assert search(context, query=query, limit=limit).is_error
    assert not transport["requests"]


@pytest.mark.parametrize("body,message", [
    ('<form id="challenge-form" action="anomaly.js">Solve</form>', "challenge"),
    ("<html>Service is temporarily unavailable</html>", "unrecognized"),
    ("<a class='result__a' href='file:///etc/passwd'>Secret</a>", "unrecognized"),
])
def test_challenges_and_unknown_markup_are_not_empty_success(context, transport, body, message):
    transport["body"] = body
    result = search(context)
    assert result.is_error and message in result.as_text()


def test_real_no_results_is_success(context, transport):
    transport["body"] = '<div class="no-results">No results found.</div>'
    result = search(context)
    assert not result.is_error and json.loads(result.as_text())["results"] == []


def test_search_results_about_captchas_are_not_challenges(context, transport):
    transport["body"] = HTML.replace("Official Python", "CAPTCHA implementation in Python")
    result = search(context, query="Python CAPTCHA")
    assert not result.is_error
    assert "CAPTCHA implementation" in json.loads(result.as_text())["results"][0]["snippet"]


@pytest.mark.parametrize("status,message", [(202, "challenge"), (429, "rate limit"), (403, "denied"), (503, "HTTP 503"), (302, "HTTP 302")])
def test_service_errors_and_redirects(context, transport, status, message):
    transport["status"] = status
    result = search(context)
    assert result.is_error and message in result.as_text()
    assert len(transport["requests"]) == 1


def test_response_size_bound_and_snippet_bound(context, transport):
    transport["body"] = HTML + "x" * MAX_BYTES
    result = search(context)
    assert result.is_error and "1 MiB" in result.as_text()
    transport["body"] = '<a class="result__a" href="https://example.org">Title</a><a class="result__snippet">' + "a" * 3000 + "</a>"
    assert len(json.loads(search(context).as_text())["results"][0]["snippet"]) == 1000


@pytest.mark.parametrize("error,message", [(httpx.ReadTimeout("private details"), "timed out"), (httpx.ConnectError("private details"), "connect")])
def test_network_errors_are_safe_tool_results(context, transport, error, message):
    transport["error"] = error
    result = search(context)
    assert result.is_error and message in result.as_text() and "private details" not in result.as_text()


def test_cancel_before_request(context, transport):
    context.signal.set()
    result = search(context)
    assert result.is_error and "cancelled" in result.as_text()
    assert not transport["requests"]


def test_cancel_during_stream_closes_response(context, transport):
    class Stream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b" " * 65536
            context.signal.set()
            yield HTML.encode()

        def close(self):
            self.closed = True

    transport["stream"] = stream = Stream()
    result = search(context)
    assert result.is_error and "cancelled" in result.as_text()
    assert stream.closed


def test_deepseek_post_credentials_and_source_results(context, transport, monkeypatch):
    monkeypatch.setenv("SEARCH_TEST_TOKEN", "private-token")
    transport["body"] = json.dumps({"results": [
        {"title": "Source", "url": "https://example.com/article", "summary": "Full summary", "snippet": "Short"},
        {"title": "Same", "url": "https://example.com/article"},
        {"title": "Invalid", "url": "javascript:alert(1)"},
        {"title": "Second", "url": "https://example.org/", "summary": "", "snippet": "Fallback summary"},
        {"title": "Third", "url": "https://example.net/", "summary": "Omitted by limit"},
    ]})
    result = search(context, {"backend": "deepseek", "apiKeyEnv": "SEARCH_TEST_TOKEN"}, query="  Python 中文 docs  ", limit=2)
    assert not result.is_error
    assert json.loads(result.as_text())["results"] == [
        {"title": "Source", "url": "https://example.com/article", "snippet": "Full summary"},
        {"title": "Second", "url": "https://example.org/", "snippet": "Fallback summary"},
    ]
    request = transport["requests"][0]
    assert request.method == "POST" and str(request.url) == "https://api.deepseek.com/v1/tools/search"
    assert request.headers["Authorization"] == "Bearer private-token"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {"query": "Python 中文 docs", "count": 2, "need_content": False}
    assert result.details == {"count": 2, "backend": "deepseek"}
    assert "private-token" not in result.as_text()


@pytest.mark.parametrize("key", ["", "  "])
def test_deepseek_missing_key_does_not_send_request(context, transport, monkeypatch, key):
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    result = search(context, {"backend": "deepseek"})
    assert result.is_error and "DEEPSEEK_API_KEY" in result.as_text() and "/login api deepseek" in result.as_text()
    assert not transport["requests"]


@pytest.mark.parametrize("body", ["not json", "[]", "{}", '{"results":"invalid"}', '{"error":{"message":"private details"}}'])
def test_deepseek_malformed_response(context, transport, monkeypatch, body):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "token")
    transport["body"] = body
    result = search(context, {"backend": "deepseek"})
    assert result.is_error and "invalid search response" in result.as_text()
    assert "private details" not in result.as_text()


def test_deepseek_empty_results_are_success(context, transport, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "token")
    transport["body"] = '{"results": []}'
    result = search(context, {"backend": "deepseek"})
    assert not result.is_error and json.loads(result.as_text())["results"] == []


def test_deepseek_unicode_query_length_is_checked_without_truncation(context, transport, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "token")
    transport["body"] = '{"results": []}'
    query = "中" * 128
    result = search(context, {"backend": "deepseek"}, query=query)
    assert not result.is_error
    assert json.loads(transport["requests"][0].content)["query"] == query
    result = search(context, {"backend": "deepseek"}, query=query + "文")
    assert result.is_error and "128 characters" in result.as_text()
    assert len(transport["requests"]) == 1


@pytest.mark.parametrize("status,message", [(401, "denied"), (402, "insufficient balance"), (429, "rate limit"), (500, "HTTP 500"), (307, "HTTP 307")])
def test_deepseek_errors_do_not_retry_or_reveal_response(context, transport, monkeypatch, status, message):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-token")
    transport.update(status=status, body="private-token private details")
    result = search(context, {"backend": "deepseek"})
    assert result.is_error and message in result.as_text()
    assert "private" not in result.as_text()
    assert len(transport["requests"]) == 1


def test_deepseek_result_bounds_and_invalid_rows(context, transport, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "token")
    transport["body"] = json.dumps({"results": [
        None, [], {"title": "Invalid", "url": 123}, {"title": 123, "url": "https://example.com/"},
        {"title": "Credentials", "url": "https://user:password@example.com/"},
        {"title": "x" * 500, "url": "https://example.org/", "summary": "y" * 2000},
        {"title": "No summary", "url": "https://example.net/", "summary": [], "snippet": {}},
    ]})
    result = search(context, {"backend": "deepseek"})
    rows = json.loads(result.as_text())["results"]
    assert rows == [
        {"title": "x" * 300, "url": "https://example.org/", "snippet": "y" * 1000},
        {"title": "No summary", "url": "https://example.net/", "snippet": ""},
    ]
    transport["body"] = "x" * (MAX_BYTES + 1)
    result = search(context, {"backend": "deepseek"})
    assert result.is_error and "1 MiB" in result.as_text()


def test_execution_resolves_backend_and_credentials_again_each_time(context, transport, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-environment-key")
    current = {"backend": "duckduckgo", "key": "first-key"}
    key_contexts = []

    def api_key(ctx):
        key_contexts.append(ctx)
        return current["key"]

    tool = make_web_search({}, resolve_backend=lambda ctx: current["backend"], resolve_api_key=api_key)
    assert not tool.execute({"query": "Python"}, context).is_error
    assert key_contexts == []
    current["backend"] = "deepseek"
    transport["body"] = '{"results": []}'
    assert not tool.execute({"query": "Python"}, context).is_error
    current["key"] = "second-key"
    assert not tool.execute({"query": "Python"}, context).is_error
    assert [request.headers.get("authorization") for request in transport["requests"]] == [
        None, "Bearer first-key", "Bearer second-key",
    ]
    assert key_contexts == [context, context]
    current["key"] = ""
    assert tool.execute({"query": "Python"}, context).is_error
    assert len(transport["requests"]) == 3


def test_removed_backend_never_sends_request(context, transport):
    result = search(context, {"backend": "brave"})
    assert result.is_error and "'duckduckgo' or 'deepseek'" in result.as_text()
    assert not transport["requests"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), "wrong"])
def test_invalid_timeout_never_sends_request(context, transport, timeout):
    assert search(context, {"timeout": timeout}).is_error
    assert not transport["requests"]


def test_timeout_clamped(context, transport):
    assert not search(context, {"timeout": 1000}).is_error
    assert transport["timeout"] == 30
