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


def test_brave_credentials_and_source_results(context, transport, monkeypatch):
    monkeypatch.setenv("SEARCH_TEST_TOKEN", "private-token")
    transport["body"] = json.dumps({"web": {"results": [
        {"title": "Source", "url": "https://example.com/article", "description": "Summary"},
        {"title": "Same", "url": "https://example.com/article"},
        {"title": "Invalid", "url": "javascript:alert(1)"},
    ]}})
    result = search(context, {"backend": "brave", "apiKeyEnv": "SEARCH_TEST_TOKEN"}, limit=2)
    assert not result.is_error
    assert json.loads(result.as_text())["results"] == [
        {"title": "Source", "url": "https://example.com/article", "snippet": "Summary"},
    ]
    request = transport["requests"][0]
    assert request.url.host == "api.search.brave.com"
    assert request.headers["X-Subscription-Token"] == "private-token"
    assert request.url.params["count"] == "2"
    assert "private-token" not in result.as_text()


def test_brave_missing_key_does_not_send_request(context, transport, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    result = search(context, {"backend": "brave"})
    assert result.is_error and "BRAVE_SEARCH_API_KEY" in result.as_text()
    assert not transport["requests"]


@pytest.mark.parametrize("body", ["not json", "[]", "{}", '{"web":{"results":"invalid"}}', '{"error":{"message":"no"}}'])
def test_brave_malformed_response(context, transport, monkeypatch, body):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "token")
    transport["body"] = body
    result = search(context, {"backend": "brave"})
    assert result.is_error and "invalid search response" in result.as_text()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), "wrong"])
def test_invalid_timeout_never_sends_request(context, transport, timeout):
    assert search(context, {"timeout": timeout}).is_error
    assert not transport["requests"]


def test_timeout_clamped(context, transport):
    assert not search(context, {"timeout": 1000}).is_error
    assert transport["timeout"] == 30
