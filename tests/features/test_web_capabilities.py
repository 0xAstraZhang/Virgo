"""Exercise capability selection through real Session requests, independent of vendor names."""

from dataclasses import replace

import pytest

from virgo.ai import web
from virgo.ai.types import AssistantMessage, Model, TextContent, UserMessage
from virgo.harness.session import Runtime

from tests.support.harness import install_responses


def context(session, model):
    session.main.model = model
    session.main.messages.append(UserMessage("Read https://example.com/article and cite it."))
    return session.main.request_context(session.main.messages)


@pytest.mark.parametrize("model", [
    Model("openai", "gpt-6-luna", "openai"),
    Model("openai-codex", "gpt-6.1-terra", "openai-codex"),
    Model("xai", "grok-4.3", "openai-responses", "https://api.x.ai/v1"),
])
def test_bundled_native_reading_hides_http_fetch(make_session, model):
    ctx = context(make_session(), model)
    assert len(ctx.web_tools) == 1
    assert ctx.web_tools[0]["type"] == "web_search"
    assert not {"web_search", "web_fetch"} & {t.name for t in ctx.tools}


@pytest.mark.parametrize("covers", [frozenset({"fetch"}), frozenset({"search", "fetch"}), frozenset({"search"})])
def test_unknown_vendor_adapts_without_planner_changes(make_session, monkeypatch, covers):
    offer = web.WebOffer("new-vendor-reader", covers, declaration={"type": "new_web_tool"})
    monkeypatch.setattr(web, "ADAPTERS", [lambda model: [offer]])
    ctx = context(make_session(), Model("future-vendor", "future-model", "faux"))
    assert ctx.web_tools == [{"type": "new_web_tool"}]
    names = {t.name for t in ctx.tools}
    assert ("web_fetch" in names) is ("fetch" not in covers)
    assert ("web_search" in names) is ("search" not in covers)


@pytest.mark.parametrize("settings", [
    {"search": {"mode": "off"}},
    {"fetch": {"mode": "off"}},
    {"fetch": {"mode": "builtin", "backend": "http"}},
    {"features": {"fetch": False}},
    {"permissions": {"deny": ["web_fetch(*private*)"]}},
])
def test_bundled_offer_is_removed_when_any_capability_conflicts(make_session, settings):
    ctx = context(make_session(settings=settings, mode="plan"), Model("openai", "gpt-6-luna", "openai"))
    assert ctx.web_tools == []
    assert ctx.web_search is None
    assert ("web_fetch" in {t.name for t in ctx.tools}) is not (
        settings.get("fetch", {}).get("mode") == "off" or settings.get("features", {}).get("fetch") is False
    )


def test_native_fetch_false_excludes_combined_offer(make_session):
    ctx = context(make_session(), Model("openai", "gpt-6-luna", "openai", options={"nativeFetch": False}))
    assert not ctx.web_tools
    assert {"web_search", "web_fetch"} <= {t.name for t in ctx.tools}


def test_native_only_reports_combination_conflict(make_session):
    s = make_session(settings={"search": {"mode": "off"}, "fetch": {"mode": "native"}})
    s.main.model = Model("openai", "gpt-6-luna", "openai")
    result = s.main.call_model([], s.main.agent.signal).result()
    assert result.stop_reason == "error" and "Native web fetch" in result.error_message


def test_restricted_role_cannot_inherit_combined_tool(make_session):
    s = make_session()
    worker = Runtime(s, "reader", "subagent", model=Model("openai", "gpt-6-luna", "openai"),
                     tools=[s.tool("web_fetch")], compaction=False)
    ctx = worker.request_context([UserMessage("Read a URL")])
    assert not ctx.web_tools
    assert [t.name for t in ctx.tools] == ["web_fetch"]


def test_anthropic_independent_tools_and_fetch_off(make_session):
    s = make_session()
    ctx = context(s, Model("anthropic", "claude-sonnet-4-6", "anthropic"))
    assert {d["name"] for d in ctx.web_tools} == {"web_search", "web_fetch"}
    fetch = next(d for d in ctx.web_tools if d["name"] == "web_fetch")
    assert fetch["citations"] == {"enabled": True}
    assert "max_uses" not in fetch and fetch["max_content_tokens"] == 8000
    s.settings.set("fetch.mode", "off")
    assert [d["name"] for d in s.main.request_context([]).web_tools] == ["web_search"]


def test_combination_fallback_is_cached_and_no_duplicate_tools(make_session, monkeypatch):
    s = make_session(settings={"compaction": {"enabled": False}})
    s.main.model = Model("openai", "gpt-6-luna", "openai")
    seen = install_responses(monkeypatch,
        AssistantMessage(stop_reason="error", error_kind="search_unsupported", error_message="unsupported web_search"),
        AssistantMessage(content=[TextContent("Cannot read yet")]),
        AssistantMessage(content=[TextContent("Still available")]),
    )
    s.prompt("Read https://example.com")
    s.prompt("Continue")
    assert seen[0][1].web_tools
    for _, ctx in seen[1:]:
        assert not ctx.web_tools
        assert {"web_fetch", "web_search"} <= {t.name for t in ctx.tools}
    # Changing the actual model invalidates the cached capability rejection.
    s.main.model = replace(s.main.model, id="gpt-6-astra")
    assert s.main.request_context([]).web_tools


def test_native_fetch_started_disables_model_retry(make_session, monkeypatch):
    s = make_session(settings={"compaction": {"enabled": False}})
    s.main.model = Model("anthropic", "claude-sonnet-4-6", "anthropic")
    seen = install_responses(monkeypatch, AssistantMessage(
        stop_reason="error", error_kind="retryable", error_message="disconnected", native_started=True,
        content=[TextContent("Partial fetch")],
    ))
    s.prompt("Read https://example.com")
    assert len(seen) == 1


def test_fetch_command_persists_without_chat_message(make_session):
    s = make_session()
    s.prompt("/fetch http")
    assert s.settings.get("fetch.mode") == "builtin"
    assert s.settings.get("fetch.backend") == "http"
    assert "saved" in s.ui.text
    assert not s.main.messages
    s.prompt("/fetch off")
    assert "web_fetch" not in {t.name for t in s.main.request_context([]).tools}
    s.prompt("/fetch auto")
    assert "web_fetch" in {t.name for t in s.main.request_context([]).tools}
