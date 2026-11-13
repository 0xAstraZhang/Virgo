"""Provider search routing exercises client tools with real session credentials."""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import httpx
import pytest

from virgo.agent.types import ToolContext
from virgo.ai.types import AssistantMessage, Model, TextContent, ToolCall, ToolResultMessage, UserMessage
from virgo.harness.session import Runtime

from tests.support.harness import install_responses


@pytest.fixture(autouse=True)
def no_real_search_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.fixture
def search_http(monkeypatch):
    state = {"requests": [], "catalog_requests": [], "status": 200}
    original = httpx.Client

    def handler(request):
        # httpx is a shared module: patching the web tool's Client also catches
        # startup catalog discovery. Keep its traffic out of search assertions.
        if request.method == "GET" and request.url.path.endswith("/models"):
            state["catalog_requests"].append(request)
            return httpx.Response(200, json={"data": [
                {"id": "deepseek-flash", "context_window": 123456},
            ]})
        state["requests"].append(request)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(state["status"], json={"results": [
                {"title": "Python docs", "url": "https://docs.python.org/", "summary": "Documentation"},
            ]})
        assert request.url.host == "html.duckduckgo.com"
        assert "authorization" not in request.headers
        return httpx.Response(200, text='<a class="result__a" href="https://python.org/">Python</a>')

    monkeypatch.setattr("virgo.tools.web.httpx.Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handler)))
    return state


def execute_search(session, runtime=None):
    runtime = runtime or session.main
    ctx = ToolContext("search", runtime.cwd, runtime.agent.signal, runtime.name, review_source=runtime.review_source)
    return session.tool("web_search").execute({"query": "Python docs"}, ctx)


@pytest.mark.parametrize("denied", [False, True], ids=["allowed", "denied"])
def test_startup_catalog_refresh_does_not_count_as_search(make_session, search_http, monkeypatch, denied):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session(mode="auto-review" if denied else "accept-all", settings={
        "permissions": {"deny": ["web_search(*private*)"]}, "compaction": {"enabled": False},
    })
    session.set_model("deepseek/deepseek-flash")
    # Force the background request to finish before any search assertion. This
    # must exercise startup discovery, rather than win a race against its thread.
    assert session._catalog_refresh_done.wait(3)
    assert not search_http["requests"]
    assert session.model.context_window == 123456
    query = "private information" if denied else "Python docs"
    install_responses(monkeypatch,
        AssistantMessage(content=[ToolCall("s", "web_search", {"query": query})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Handled")]),
    )
    session.prompt("Search for documentation")
    assert [str(request.url) for request in search_http["catalog_requests"]] == [
        "https://api.deepseek.com/models",
    ]
    assert [str(request.url) for request in search_http["requests"]] == (
        [] if denied else ["https://api.deepseek.com/v1/tools/search"]
    )
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert result.is_error is denied


@pytest.mark.parametrize("mode", ["auto", "native"])
def test_deepseek_native_uses_official_search_and_saved_login(make_session, search_http, mode):
    session = make_session(settings={"search": {"mode": mode}})
    session.set_model("deepseek/deepseek-flash")
    session.auth.set("deepseek", {
        "type": "api_key", "key": "saved-ds-key",
        "connection": {"api": "openai", "baseUrl": "https://api.deepseek.com"},
    })
    context = session.main.request_context([])
    assert context.web_search is None  # Standalone API, not a hosted model-tool declaration.
    tool = next(tool for tool in context.tools if tool.name == "web_search")
    assert tool.parameters["properties"]["query"]["maxLength"] == 128
    result = execute_search(session)
    assert not result.is_error
    assert json.loads(result.as_text())["backend"] == "deepseek"
    request, = search_http["requests"]
    assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
    assert request.headers["authorization"] == "Bearer saved-ds-key"
    assert "saved-ds-key" not in result.as_text()


@pytest.mark.parametrize("base_url", [
    "https://proxy.example/v1", "https://api.deepseek.com.evil.example",
    "http://api.deepseek.com", "https://api.deepseek.com:8443", "https://api.deepseek.com/proxy",
])
def test_deepseek_provider_name_does_not_enable_native_on_proxies(make_session, search_http, base_url):
    session = make_session(settings={"providers": {"deepseek": {"baseUrl": base_url, "apiKey": "proxy-secret"}}})
    session.set_model("deepseek/deepseek-flash")
    context = session.main.request_context([])
    assert context.web_search is None
    assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
    with pytest.raises(ValueError, match="/login api deepseek"):
        session.services["search"].set_selection("deepseek")
    assert all(request.url.host == "html.duckduckgo.com" for request in search_http["requests"])


def test_no_native_search_uses_free_backend_even_with_deepseek_key(make_session, search_http, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session()
    session.main.request_context([])
    assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
    session.set_model("deepseek/deepseek-flash")
    session.main.model.options["nativeSearch"] = False
    session.main.request_context([])
    assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"


def test_explicit_search_backend_overrides_hosted_native(make_session, search_http, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session()
    session.model = session.main.model = Model("anthropic", "claude-sonnet-4-6", "anthropic")
    assert session.main.request_context([]).web_search
    session.services["search"].set_selection("deepseek")
    assert session.main.request_context([]).web_search is None
    assert json.loads(execute_search(session).as_text())["backend"] == "deepseek"
    session.services["search"].set_selection("duckduckgo")
    assert session.main.request_context([]).web_search is None
    assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
    session.services["search"].set_selection("auto")
    assert session.main.request_context([]).web_search


def test_native_missing_credentials_and_auto_fallback(make_session, search_http):
    session = make_session()
    session.set_model("deepseek/deepseek-flash")
    session.main.request_context([])
    assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
    session.settings.set("search.mode", "native")
    with pytest.raises(ValueError, match="/login api deepseek"):
        session.main.request_context([])


def test_saved_credentials_must_match_official_connection(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    session = make_session()
    session.auth.set("deepseek", {
        "type": "api_key", "key": "proxy-key",
        "connection": {"api": "openai", "baseUrl": "https://proxy.example"},
    })
    with pytest.raises(ValueError, match="/login api deepseek"):
        session.services["search"].set_selection("deepseek")
    assert session.services["search"].selection == "auto"


def test_explicit_search_key_is_separate_from_proxy_credentials(make_session, search_http, monkeypatch):
    monkeypatch.setenv("SEARCH_KEY", "official-search-key")
    session = make_session(settings={
        "search": {"apiKeyEnv": "SEARCH_KEY"},
        "providers": {"deepseek": {"baseUrl": "https://proxy.example", "apiKey": "proxy-key"}},
    })
    session.set_model("deepseek/deepseek-flash")
    session.services["search"].set_selection("deepseek")
    session.main.request_context([])
    assert not execute_search(session).is_error
    request, = search_http["requests"]
    assert request.headers["authorization"] == "Bearer official-search-key"


def test_legacy_brave_key_is_never_reused_for_deepseek(make_session, search_http, monkeypatch):
    monkeypatch.setenv("OLD_SEARCH_KEY", "brave-secret")
    session = make_session(settings={"search": {
        "mode": "builtin", "backend": "brave", "apiKeyEnv": "OLD_SEARCH_KEY",
    }})
    assert session.services["search"].selection == "duckduckgo"
    assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
    session.prompt("/search ds")
    assert session.services["search"].selection == "duckduckgo"
    assert "/login api deepseek" in session.ui.text
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session.prompt("/search ds")
    assert session.services["search"].selection == "deepseek"
    assert not execute_search(session).is_error
    assert search_http["requests"][-1].headers["authorization"] == "Bearer ds-key"
    saved = json.loads((session.paths.user / "settings.json").read_text())
    assert saved["search"]["apiKeyEnv"] is None
    assert "brave-secret" not in session.ui.text


def test_official_alias_reuses_its_own_key_and_respects_key_rotation(make_session, search_http):
    session = make_session(settings={"providers": {"ds-alias": {
        "api": "openai", "baseUrl": "https://api.deepseek.com/v1",
    }}})
    session.set_model("ds-alias/deepseek-flash")
    session.auth.set("ds-alias", {"type": "api_key", "key": "first-key"})
    session.main.request_context([])
    assert not execute_search(session).is_error
    session.auth.set("ds-alias", {"type": "api_key", "key": "second-key"})
    assert not execute_search(session).is_error
    assert [r.headers["authorization"] for r in search_http["requests"]] == ["Bearer first-key", "Bearer second-key"]


def test_interleaved_agents_use_their_own_request_model(make_session, search_http, monkeypatch):
    from virgo.harness import preferences

    catalog_ready = threading.Event()
    release_catalog = threading.Event()
    original_fetch = preferences.fetch_model_catalog

    def delayed_catalog(model, signal):
        catalog_ready.set()
        assert release_catalog.wait(3)
        return original_fetch(model, signal)

    monkeypatch.setattr(preferences, "fetch_model_catalog", delayed_catalog)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session()
    try:
        assert catalog_ready.wait(3)
        ds = session.resolve("deepseek/deepseek-flash")
        worker = Runtime(session, "researcher", "subagent", model=ds, compaction=False)
        worker.request_context([])
        session.main.request_context([])
        assert json.loads(execute_search(session, worker).as_text())["backend"] == "deepseek"
        assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
        # Finish an unrelated provider refresh after preparing a retry's model.
        # It must not invalidate the request binding of the unchanged main model.
        session.main.request_context([], ds)
        release_catalog.set()
        assert session._catalog_refresh_done.wait(3)
        assert json.loads(execute_search(session).as_text())["backend"] == "deepseek"
        session.set_model("faux/other")
        session.main.request_context([])
        assert json.loads(execute_search(session).as_text())["backend"] == "duckduckgo"
        assert next(t for t in worker.request_context([]).tools if t.name == "web_search").parameters["properties"]["query"]["maxLength"] == 128
        assert next(t for t in session.main.request_context([]).tools if t.name == "web_search").parameters["properties"]["query"]["maxLength"] == 2000
    finally:
        release_catalog.set()
        assert session._catalog_refresh_done.wait(3)


@pytest.mark.parametrize("status", [200, 401, 429, 503])
def test_deepseek_tool_loop_keeps_results_or_error_without_other_backend(make_session, search_http, monkeypatch, status):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session(settings={"compaction": {"enabled": False}})
    session.set_model("deepseek/deepseek-flash")
    search_http["status"] = status
    seen = install_responses(monkeypatch,
        AssistantMessage(content=[ToolCall("s", "web_search", {"query": "Python docs"})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Search handled")]),
    )
    session.prompt("Find public Python documentation")
    assert all(context.web_search is None for _, context in seen)
    assert len(search_http["requests"]) == 1
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert result.is_error is (status != 200)
    if status == 200:
        assert "https://docs.python.org/" in result.text()


def test_deepseek_native_honors_exact_query_denial(make_session, search_http, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session(mode="auto-review", settings={
        "permissions": {"deny": ["web_search(*private*)"]}, "compaction": {"enabled": False},
    })
    session.set_model("deepseek/deepseek-flash")
    monkeypatch.setattr("virgo.features.permission.review_tool_call", lambda *args: pytest.fail("denial must precede review"))
    install_responses(monkeypatch,
        AssistantMessage(content=[ToolCall("s", "web_search", {"query": "private information"})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Denied")]),
    )
    session.prompt("Search private information")
    assert not search_http["requests"]
    assert next(m for m in session.main.messages if isinstance(m, ToolResultMessage)).is_error


def test_restricted_subagent_and_custom_tool_keep_their_scope(make_session, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    session = make_session()
    ds = session.resolve("deepseek/deepseek-flash")
    restricted = Runtime(session, "restricted", "subagent", model=ds, tools=[session.tool("read")], compaction=False)
    context = restricted.request_context([UserMessage("Search")])
    assert context.web_search is None and not any(t.name == "web_search" for t in context.tools)
    custom = replace(session.tool("web_search"), description="Extension search", parameters={"type": "object"})
    worker = Runtime(session, "custom", "subagent", model=ds, tools=[custom], compaction=False)
    context = worker.request_context([])
    assert context.tools[0].description == "Extension search"
    assert context.tools[0].parameters == {"type": "object"}
