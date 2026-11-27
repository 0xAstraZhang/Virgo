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
    {"plugins": {"web": {"fetchEnabled": False}}},
])
def test_bundled_offer_is_removed_when_any_capability_conflicts(make_session, settings):
    ctx = context(make_session(settings=settings), Model("openai", "gpt-6-luna", "openai"))
    assert ctx.web_tools == []
    assert ("web_fetch" in {t.name for t in ctx.tools}) is not (
        settings.get("fetch", {}).get("mode") == "off"
        or settings.get("plugins", {}).get("web", {}).get("fetchEnabled") is False
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


def test_web_guidance_injected_for_client_and_hosted_tools_and_omitted_when_off(make_session):
    from virgo.plugins.web import GUIDANCE

    # DeepSeek client-side tools
    s_ds = make_session()
    s_ds.auth.set("deepseek", {"type": "api_key", "key": "official-key"})
    ctx_ds = context(s_ds, Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com"))
    assert not ctx_ds.web_tools
    assert {"web_search", "web_fetch"} <= {t.name for t in ctx_ds.tools}
    assert GUIDANCE in ctx_ds.system
    assert "Prefer available web tools over shell commands" in ctx_ds.system

    # Only web_fetch enabled (client-side)
    s_fetch = make_session(settings={"search": {"mode": "off"}})
    ctx_fetch = context(s_fetch, Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com"))
    assert GUIDANCE in ctx_fetch.system

    # Hosted web tools
    s_hosted = make_session()
    ctx_hosted = context(s_hosted, Model("anthropic", "claude-sonnet-4-6", "anthropic"))
    assert ctx_hosted.web_tools
    assert GUIDANCE in ctx_hosted.system

    # Both disabled: guidance omitted
    s_off = make_session(settings={"search": {"mode": "off"}, "fetch": {"mode": "off"}})
    ctx_off = context(s_off, Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com"))
    assert GUIDANCE not in ctx_off.system


@pytest.mark.parametrize("model", [
    Model("openai", "gpt-6-luna", "openai"),
    Model("openai-codex", "gpt-6.1-terra", "openai-codex"),
    Model("xai", "grok-4.3", "openai-responses", "https://api.x.ai/v1"),
])
@pytest.mark.parametrize("settings,expected_setting", [
    ({"search": {"maxUses": 2}}, "search.maxUses"),
    ({"fetch": {"maxUses": 1}}, "fetch.maxUses"),
])
def test_unmapped_explicit_max_uses_falls_back_to_client_tools(make_session, model, settings, expected_setting):
    s = make_session(settings=settings)
    ctx = context(s, model)
    assert ctx.web_tools == []
    assert {"web_search", "web_fetch"} <= {t.name for t in ctx.tools}
    paths = s.services["web"].paths_for(s.main)
    assert expected_setting in paths["search"]
    assert expected_setting in paths["fetch"]


def test_unmapped_max_uses_only_rejects_hosted_offers_covering_that_capability(make_session):
    # gpt-4o hosted web_search only covers search, not fetch, so fetch.maxUses leaves it hosted.
    model = Model("openai", "gpt-4o", "openai")
    ctx_fetch_cap = context(make_session(settings={"fetch": {"maxUses": 1}}), model)
    assert ctx_fetch_cap.web_tools == [{"type": "web_search"}]
    names = {t.name for t in ctx_fetch_cap.tools}
    assert "web_fetch" in names and "web_search" not in names

    ctx_search_cap = context(make_session(settings={"search": {"maxUses": 2}}), model)
    assert ctx_search_cap.web_tools == []
    assert {"web_search", "web_fetch"} <= {t.name for t in ctx_search_cap.tools}


@pytest.mark.parametrize("capability,settings", [
    ("search", {"search": {"mode": "native", "maxUses": 2}}),
    ("fetch", {"fetch": {"mode": "native", "maxUses": 1}}),
])
def test_unmapped_explicit_max_uses_fails_closed_in_native_mode(make_session, monkeypatch, capability, settings):
    s = make_session(settings=settings)
    s.main.model = Model("openai", "gpt-6-luna", "openai")
    dispatched = install_responses(monkeypatch)
    result = s.main.call_model([], s.main.agent.signal).result()
    assert not dispatched
    assert result.stop_reason == "error"
    assert f"Native web {capability}" in result.error_message
    assert f"{capability}.maxUses" in result.error_message


def test_mapped_explicit_max_uses_stays_hosted_on_anthropic(make_session):
    s = make_session(settings={"search": {"maxUses": 2}, "fetch": {"maxUses": 1}})
    ctx = context(s, Model("anthropic", "claude-sonnet-4-6", "anthropic"))
    by_name = {d["name"]: d for d in ctx.web_tools}
    assert set(by_name) == {"web_search", "web_fetch"}
    assert by_name["web_search"]["max_uses"] == 2
    assert by_name["web_fetch"]["max_uses"] == 1
    assert not {"web_search", "web_fetch"} & {t.name for t in ctx.tools}


def test_runtime_max_uses_toggle_switches_between_hosted_and_client(make_session):
    s = make_session()
    model = Model("openai-codex", "gpt-6.1-terra", "openai-codex")
    assert context(s, model).web_tools == [{"type": "web_search"}]
    s.settings.set("search.maxUses", 1)
    ctx_capped = s.main.request_context(s.main.messages)
    assert ctx_capped.web_tools == []
    assert {"web_search", "web_fetch"} <= {t.name for t in ctx_capped.tools}
    s.settings.set("search.maxUses", None)
    assert s.main.request_context(s.main.messages).web_tools == [{"type": "web_search"}]
