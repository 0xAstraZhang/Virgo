"""Search selection and fallback exercise the real session/tool loop without credentials."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from virgo import ai
from virgo.agent.types import AgentTool, ToolCallEvent, ToolResult
from virgo.ai.providers import openai
from virgo.ai.providers._http import ProviderError
from virgo.ai.search import add_source_links, is_search_unsupported
from virgo.ai.transform import normalize
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    RawContent,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
)
from virgo.features.permission import ReviewDecision
from virgo.features.search import native_tool
from virgo.harness.session import Runtime

from tests.support.harness import install_responses, response_stream


def selected_model(**overrides):
    return replace(Model("anthropic", "claude-sonnet-4-6", "anthropic"), **overrides)


def select(session, model=None):
    session.main.model = model or selected_model()
    session.main.messages.append(UserMessage("Search the public Python documentation."))
    return session.main


@pytest.mark.parametrize("model,expected", [
    (selected_model(), "web_search_20250305"),
    (Model("openai", "gpt-6-luna", "openai"), "web_search"),
    (Model("openai", "gpt-5.4", "openai-responses"), "web_search"),
    (Model("openai-codex", "gpt-6.1-terra", "openai-codex"), "web_search"),
    (Model("xai", "grok-4.3", "openai-responses", "https://api.x.ai/v1"), "web_search"),
    (selected_model(base_url="https://proxy.example/v1"), None),
    (Model("openai", "gpt-6-luna", "openai", "https://proxy.example/v1"), None),
    (Model("openai-codex", "gpt-6.1-terra", "openai-codex", "https://proxy.example"), None),
    (selected_model(base_url="https://api.anthropic.com.evil.example"), None),
    (selected_model(base_url="http://api.anthropic.com"), None),
    (selected_model(base_url="https://api.anthropic.com:8443"), None),
    (selected_model(base_url="https://user@api.anthropic.com"), None),
    (Model("openai", "gpt-6-realtime", "openai"), None),
    (Model("google-gemini-cli", "gemini-2.5-pro", "google-gemini-cli"), None),
    (Model("deepseek", "deepseek-flash", "openai", "https://api.deepseek.com"), None),
    (selected_model(options={"nativeSearch": False}), None),
    (selected_model(base_url="https://proxy.example", options={"nativeSearch": True}), "web_search_20250305"),
])
def test_native_selection_checks_protocol_host_and_model(model, expected, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    tool = native_tool(model)
    assert (tool or {}).get("type") == expected


@pytest.mark.parametrize("env,model", [
    ("ANTHROPIC_BASE_URL", selected_model()),
    ("OPENAI_BASE_URL", Model("openai", "gpt-6-luna", "openai")),
])
def test_environment_proxy_prevents_assuming_official_search(env, model, monkeypatch):
    monkeypatch.setenv(env, "https://proxy.example/v1")
    assert native_tool(model) is None


@pytest.mark.parametrize("mode,native,builtin", [
    ("auto", True, False), ("native", True, False),
    ("builtin", False, True), ("off", False, False),
])
def test_modes_offer_exactly_one_search_path(make_session, mode, native, builtin):
    session = make_session(settings={"search": {"mode": mode}})
    runtime = select(session)
    context = runtime.request_context(runtime.messages)
    assert bool(context.web_search) is native
    assert any(t.name == "web_search" for t in context.tools) is builtin


def test_native_only_rejects_unsupported_model_without_silent_fallback(make_session):
    session = make_session(settings={"search": {"mode": "native"}})
    result = session.main.call_model([], session.main.agent.signal).result()
    assert result.stop_reason == "error"
    assert "Native web search is unavailable" in result.error_message


def test_subagent_tool_scope_cannot_acquire_native_search(make_session):
    session = make_session()
    runtime = Runtime(session, "restricted", "subagent", model=selected_model(),
                      tools=[session.tool("read")], compaction=False)
    context = runtime.request_context([UserMessage("Search online")])
    assert context.web_search is None
    assert "web_search" not in [tool.name for tool in context.tools]


def test_explicit_unsupported_falls_back_executes_builtin_and_caches(make_session, monkeypatch):
    session = make_session(settings={"compaction": {"enabled": False}})
    runtime = select(session)
    calls = []

    def search(args, ctx):
        calls.append(args)
        return ToolResult.text('Python documentation: https://docs.python.org/')

    monkeypatch.setattr(session.tool("web_search"), "execute", search)
    seen = install_responses(
        monkeypatch,
        AssistantMessage(stop_reason="error", error_kind="search_unsupported", error_message="unsupported web_search"),
        AssistantMessage(content=[ToolCall("search-1", "web_search", {"query": "Python documentation"})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("See https://docs.python.org/")]),
        AssistantMessage(content=[TextContent("Follow-up answer")]),
    )
    runtime.agent.prompt("Find the Python documentation.")
    runtime.agent.prompt("Find the documentation again.")
    assert [bool(ctx.web_search) for _, ctx in seen] == [True, False, False, False]
    assert "web_search" not in [t.name for t in seen[0][1].tools]
    assert all("web_search" in [t.name for t in ctx.tools] for _, ctx in seen[1:])
    assert calls == [{"query": "Python documentation"}]
    assert any(isinstance(m, ToolResultMessage) and "docs.python.org" in m.text() for m in runtime.messages)
    assert session.ui.text.count("using builtin web_search") == 1
    assert session.store.custom("model_call")[-1]["purpose"] == "search-fallback"
    assert runtime.agent.last_text == "Follow-up answer"


def test_rejection_after_builtin_fallback_is_not_retried_again(make_session, monkeypatch):
    session = make_session(settings={"compaction": {"enabled": False}})
    runtime = select(session)
    failure = AssistantMessage(stop_reason="error", error_kind="search_unsupported", error_message="unsupported web_search")
    seen = install_responses(monkeypatch, failure, copy.deepcopy(failure))
    result = runtime.call_model(runtime.messages, runtime.agent.signal).result()
    assert result.stop_reason == "error"
    assert len(seen) == 2


@pytest.mark.parametrize("failure", [
    AssistantMessage(stop_reason="error", error_kind="fatal", error_message="401 invalid API key"),
    AssistantMessage(stop_reason="error", error_kind="retryable", error_message="429 rate limit"),
    AssistantMessage(stop_reason="error", error_kind="retryable", error_message="connection timeout"),
    AssistantMessage(content=[TextContent("No results found")], search_started=True),
    AssistantMessage(content=[TextContent("No search needed")]),
])
def test_other_errors_and_successes_do_not_poison_capability_cache(make_session, monkeypatch, failure):
    session = make_session(settings={"retry": {"maxRetries": 0}, "compaction": {"enabled": False}})
    runtime = select(session)
    seen = install_responses(monkeypatch, failure, AssistantMessage(content=[TextContent("Next")]))
    runtime.call_model(runtime.messages, runtime.agent.signal).result()
    runtime.call_model(runtime.messages, runtime.agent.signal).result()
    assert len(seen) == 2
    assert all(ctx.web_search is not None for _, ctx in seen)


@pytest.mark.parametrize("kind", ["retryable", "search_unsupported", "overflow"])
def test_started_native_search_is_not_replayed_or_downgraded(make_session, monkeypatch, kind):
    session = make_session(settings={"retry": {"maxRetries": 3}, "compaction": {"enabled": False}})
    runtime = select(session)
    failure = AssistantMessage(
        content=[RawContent("anthropic", {"type": "server_tool_use", "name": "web_search", "input": {"query": "Python"}})],
        stop_reason="error", error_kind=kind, error_message="interrupted", search_started=True,
    )
    seen = install_responses(monkeypatch, failure)
    result = runtime.call_model(runtime.messages, runtime.agent.signal).result()
    assert result is failure
    assert len(seen) == 1
    assert runtime.request_context(runtime.messages).web_search is not None


def test_cache_is_invalidated_by_endpoint_model_and_credential_changes(make_session):
    session = make_session()
    runtime = select(session)
    service = session.services["search"]
    original = runtime.model
    context = runtime.request_context(runtime.messages)
    assert service.fallback(original, context, AssistantMessage(error_kind="search_unsupported"))
    assert runtime.request_context(runtime.messages).web_search is None
    for changed in (
        replace(original, id="claude-opus-5-5"),
        replace(original, base_url="https://proxy.example", options={"nativeSearch": True}),
        replace(original, options={"apiKey": "different-test-key"}),
    ):
        assert runtime.request_context(runtime.messages, changed).web_search is not None


@pytest.mark.parametrize("mode", ["auto-review", "plan"])
def test_deny_rule_blocks_native_and_builtin_execution(make_session, monkeypatch, mode):
    session = make_session(mode=mode, settings={"fetch": {"mode": "off"}, "permissions": {"deny": ["web_search"]}})
    runtime = select(session)
    monkeypatch.setattr("virgo.features.permission.review_tool_call", lambda *a: pytest.fail("deny must precede review"))
    context = runtime.request_context(runtime.messages)
    assert context.web_search is None
    event = ToolCallEvent(call_id="s", tool_name="web_search", args={"query": "Python"},
                          tool=session.tool("web_search"), cwd=session.cwd, agent="main")
    assert session.services["permissions"].decide(event)[0] == "deny"


@pytest.mark.parametrize("kind", ["allow", "deny"])
def test_scoped_rules_keep_search_local_for_exact_query_review(make_session, monkeypatch, kind):
    session = make_session(mode="auto-review", settings={"fetch": {"mode": "off"}, "permissions": {kind: ["web_search(*private*)"]}})
    runtime = select(session)
    monkeypatch.setattr("virgo.features.permission.review_tool_call", lambda *a: pytest.fail("native scope cannot satisfy query rules"))
    context = runtime.request_context(runtime.messages)
    assert context.web_search is None
    assert "web_search" in [t.name for t in context.tools]


def test_native_permission_review_is_cached_only_for_current_request(make_session, monkeypatch):
    session = make_session(mode="auto-review", settings={"fetch": {"mode": "off"}})
    runtime = select(session)
    reviewed = []

    def review(_session, event, reason):
        reviewed.append((event, reason))
        return ReviewDecision("allow", "Public research")

    monkeypatch.setattr("virgo.features.permission.review_tool_call", review)
    assert runtime.request_context(runtime.messages).web_search
    assert runtime.request_context(runtime.messages).web_search
    assert len(reviewed) == 1
    assert reviewed[0][0].args["native"] is True
    runtime.messages.append(UserMessage("A different public research request"))
    assert runtime.request_context(runtime.messages).web_search
    assert len(reviewed) == 2


def test_unapproved_native_scope_falls_back_without_asking_user(make_session, monkeypatch):
    session = make_session(mode="auto-review")
    runtime = select(session)
    monkeypatch.setattr("virgo.features.permission.review_tool_call", lambda *a: ReviewDecision("ask", "Need exact query"))
    context = runtime.request_context(runtime.messages)
    assert context.web_search is None
    assert "web_search" in [t.name for t in context.tools]
    assert session.ui.questions == []


@pytest.mark.parametrize("event", ["tool_call", "tool_result"])
def test_custom_tool_hooks_route_search_through_client_execution(make_session, monkeypatch, event):
    session = make_session(settings={"compaction": {"enabled": False}})
    runtime = select(session)
    assert runtime.request_context(runtime.messages).web_search
    observed = []

    def hook(*args):
        observed.append(args)

    remove = session.bus.on(event, hook)
    monkeypatch.setattr(session.tool("web_search"), "execute", lambda *a: ToolResult.text("Search source"))
    seen = install_responses(
        monkeypatch,
        AssistantMessage(content=[ToolCall("s", "web_search", {"query": "Python docs"})], stop_reason="tool_use"),
        AssistantMessage(content=[TextContent("Answer")]),
    )
    runtime.agent.prompt("Search Python docs")
    assert all(context.web_search is None for _, context in seen)
    assert len(observed) == 1
    remove()
    assert runtime.request_context(runtime.messages).web_search


def test_extension_owned_search_tool_is_not_replaced_by_hosted_search(make_session):
    session = make_session()
    custom = AgentTool("web_search", "Custom search", {"type": "object"}, lambda *a: "Custom result")
    runtime = Runtime(session, "custom", "subagent", model=selected_model(), tools=[custom], compaction=False)
    context = runtime.request_context([UserMessage("Search")])
    assert context.web_search is None
    assert context.tools[0].description == "Custom search"


@pytest.mark.parametrize("status,body,text,expected", [
    (400, {"error": {"message": "web_search is not supported"}}, "Bad request", True),
    (422, {"message": "Unknown tool web_search"}, "Bad request", True),
    (None, {"code": "unsupported_tool", "message": "web_search"}, "unsupported web_search", True),
    (401, {"message": "web_search is not allowed"}, "Invalid credential", False),
    (429, {"message": "web_search not available for now"}, "Rate limit", False),
    (500, {"message": "web_search is not supported"}, "Internal error", False),
    (400, {"message": "Invalid max_uses for web_search"}, "Bad schema", False),
    (400, {"message": "Parameter max_uses is not supported for web_search"}, "Bad schema", False),
    (400, {"code": "invalid_value", "message": "Invalid value: 'web_search'. Supported values are: 'function'."}, "Bad tool enum", True),
    (400, {"message": "This model is unsupported"}, "Unknown model", False),
    (None, None, "web_search timeout", False),
])
def test_only_explicit_search_capability_rejections_trigger_fallback(status, body, text, expected):
    assert is_search_unsupported(ProviderError(text, status_code=status, body=body)) is expected


def test_citations_survive_persistence_and_provider_switch_without_native_payloads():
    citation = {"type": "web_search_result_location", "url": "https://docs.python.org/", "title": "Python docs", "encrypted_index": "secret-index"}
    message = AssistantMessage(
        api="anthropic", provider="anthropic", model="claude-sonnet-4-6", search_started=True,
        content=[
            RawContent("anthropic", {"type": "web_search_tool_result", "encrypted_content": "opaque-native-state"}),
            TextContent("Python documentation", citations=[citation]),
        ],
    )
    add_source_links(message)
    add_source_links(message)
    restored = message_from_dict(message_to_dict(message))
    assert restored == message
    assert restored.text().count("[Python docs]") == 1
    converted = openai.convert_messages("", normalize([restored], Model("openai", "gpt-6-luna", "openai")))
    assert "https://docs.python.org/" in converted[0]["content"]
    assert "opaque-native-state" not in str(converted)
    assert "secret-index" not in str(converted)


def test_server_pause_continues_with_native_history(make_session, monkeypatch):
    session = make_session(settings={"compaction": {"enabled": False}})
    runtime = select(session)
    paused = AssistantMessage(api="anthropic", search_started=True, stop_reason="pause_turn", content=[
        RawContent("anthropic", {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": []}),
    ])
    seen = install_responses(monkeypatch, paused, AssistantMessage(content=[TextContent("Finished research")]))
    runtime.agent.prompt("Search Python docs")
    assert len(seen) == 2
    assert paused in seen[1][1].messages
    assert all(context.web_search is not None for _, context in seen)
    assert runtime.agent.last_text == "Finished research"


def test_retryable_failure_retries_native_without_downgrade(make_session, monkeypatch):
    session = make_session(settings={"retry": {"maxRetries": 1}, "compaction": {"enabled": False}})
    runtime = select(session)
    monkeypatch.setattr(runtime.agent.signal, "wait", lambda _delay: False)
    seen = install_responses(
        monkeypatch,
        AssistantMessage(stop_reason="error", error_kind="retryable", error_message="429 rate limit"),
        AssistantMessage(content=[TextContent("Recovered")]),
    )
    result = runtime.call_model(runtime.messages, runtime.agent.signal).result()
    assert result.text() == "Recovered"
    assert len(seen) == 2
    assert all(context.web_search for _, context in seen)


def test_unified_stream_routes_openai_after_auth_and_adds_portable_sources(monkeypatch):
    model = Model("openai", "gpt-6-luna", "openai")
    declaration = {"type": "web_search"}
    context = Context("Research", [UserMessage("Python docs")], web_search=declaration)
    authenticated = []
    requests = []

    def authenticate(selected, _signal=None):
        authenticated.append(selected.api)
        return selected

    def respond(selected, ctx, _opts):
        requests.append((selected.api, ctx.web_search))
        return response_stream(AssistantMessage(api=selected.api, content=[TextContent(
            "Python docs", citations=[{"type": "url_citation", "url": "https://docs.python.org", "title": "Python"}]
        )]))

    monkeypatch.setattr("virgo.ai.auth.authenticated_model", authenticate)
    monkeypatch.setattr("virgo.ai.providers.responses.stream", respond)
    stream = ai.stream(model, context, StreamOptions())
    events = list(stream)
    result = stream.result()
    assert authenticated == ["openai"]
    assert requests == [("openai-responses", declaration)]
    assert "[Python](<https://docs.python.org>)" in result.text()
    assert any(event.type == "text_delta" and "https://docs.python.org" in event.delta for event in events)
    assert events[-1].type == "done"
    assert model.api == "openai"


def test_interrupted_search_without_text_retains_partial_evidence_but_not_unpaired_calls():
    message = AssistantMessage(
        api="anthropic", provider="anthropic", model="claude-sonnet-4-6", search_started=True,
        stop_reason="error", error_kind="retryable", content=[
            RawContent("anthropic", {"type": "server_tool_use", "id": "srv_done", "name": "web_search", "input": {"query": "Python docs"}}),
            RawContent("anthropic", {"type": "web_search_tool_result", "tool_use_id": "srv_done", "content": [
                {"type": "web_search_result", "url": "https://docs.python.org", "title": "Python", "encrypted_content": "opaque"},
            ]}),
            RawContent("anthropic", {"type": "server_tool_use", "id": "srv_partial", "name": "web_search", "input": {"query": "Python latest release"}}),
        ],
    )
    before = copy.deepcopy(message)
    cleaned = normalize([message], selected_model())
    assert len(cleaned) == 1
    assert "https://docs.python.org" in cleaned[0].text()
    assert "Python latest release" in cleaned[0].text()
    assert "partial results" in cleaned[0].text()
    raw = [b.data for b in cleaned[0].content if isinstance(b, RawContent)]
    assert any(b.get("id") == "srv_done" for b in raw)
    assert not any(b.get("id") == "srv_partial" for b in raw)
    assert message == before
    assert message.text() == ""


def test_interrupted_responses_search_keeps_query_as_text_instead_of_replaying_incomplete_item():
    message = AssistantMessage(
        api="openai-responses", provider="openai", search_started=True, stop_reason="aborted",
        content=[RawContent("openai-responses", {
            "type": "web_search_call", "id": "ws_partial", "status": "in_progress",
            "action": {"type": "search", "queries": ["Python docs"]},
        })],
    )
    cleaned = normalize([message], Model("openai", "gpt-6-luna", "openai-responses"))
    assert len(cleaned) == 1
    assert "Python docs" in cleaned[0].text()
    assert "interrupted" in cleaned[0].text()
    assert all(not isinstance(b, RawContent) for b in cleaned[0].content)


def test_same_api_provider_switch_strips_hosted_metadata_but_keeps_source_links():
    citation = {"type": "url_citation", "url": "https://docs.python.org", "title": "Python"}
    annotated = {"type": "message", "id": "msg_openai", "content": [{"type": "output_text", "annotations": [citation]}]}
    message = AssistantMessage(api="openai-responses", provider="openai", search_started=True, content=[
        ThinkingContent("Research", signature="provider-encrypted-reasoning"),
        RawContent("openai-responses", {"type": "web_search_call", "id": "ws_openai", "status": "completed"}),
        TextContent("Python docs", citations=[citation], native=annotated),
    ])
    add_source_links(message)
    before = copy.deepcopy(message)
    cleaned = normalize([message], Model("xai", "grok-4.3", "openai-responses", "https://api.x.ai/v1"))
    assert len(cleaned) == 1
    assert "[Python](<https://docs.python.org>)" in cleaned[0].text()
    assert all(not isinstance(b, RawContent) for b in cleaned[0].content)
    assert all(not isinstance(b, ThinkingContent) for b in cleaned[0].content)
    assert all(not b.citations and b.native is None for b in cleaned[0].content if isinstance(b, TextContent))
    assert message == before


def test_repeated_server_pauses_stop_after_three_continuations(make_session, monkeypatch):
    session = make_session(settings={"compaction": {"enabled": False}})
    runtime = select(session)
    seen = install_responses(monkeypatch, *[
        AssistantMessage(api="anthropic", search_started=True, stop_reason="pause_turn") for _ in range(4)
    ])
    runtime.agent.prompt("Long research task")
    assert len(seen) == 4
    assert "three continuations" in runtime.agent.last_text
