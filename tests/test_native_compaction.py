"""Compaction policy, provider dispatch and conversation continuity."""

from __future__ import annotations

import copy
import importlib

import pytest

from virgo import ai
from virgo.ai.compaction import context_model, native_api
from virgo.ai.providers import responses
from virgo.ai.stream import EventStream, StreamEvent
from virgo.ai.types import (
    AssistantMessage,
    Model,
    RawContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from virgo.harness import compaction
from virgo.harness.session import AgentSession, Runtime


def append(runtime, *messages):
    for message in messages:
        runtime.messages.append(message)
        runtime.ids.append(runtime.store.append_message(message) if runtime.store else None)


def native_result(model, text="Earlier work", output=None):
    api = native_api(model)
    if output is None:
        output = [{"type": "compaction", "content": text, "signature": "signed"}] if api == "anthropic" else [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Original request"}]},
            {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"},
        ]
    return AssistantMessage(
        api=api, provider=model.provider, model=model.id,
        content=[RawContent(api, item) for item in output], stop_reason="compaction", usage=Usage(input=90000, output=100),
    )


@pytest.mark.parametrize("model,expected", [
    (Model("anthropic", "test", "anthropic"), "anthropic"),
    (Model("openai", "test", "openai"), "openai-responses"),
    (Model("openai", "test", "openai-responses"), "openai-responses"),
    (Model("openai-codex", "test", "openai-codex"), "openai-codex"),
    (Model("custom", "test", "openai", base_url="https://api.openai.com/v1"), "openai-responses"),
    (Model("custom", "test", "anthropic", base_url="https://api.anthropic.com"), "anthropic"),
    (Model("deepseek", "test", "openai"), None),
    (Model("xai", "test", "openai-responses"), None),
    (Model("custom", "test", "anthropic", base_url="https://example.com"), None),
    (Model("faux", "test", "faux"), None),
])
def test_native_routing_is_provider_specific(model, expected):
    assert native_api(model) == expected


@pytest.mark.parametrize("provider,api", [
    ("openai", "openai"), ("anthropic", "anthropic"), ("deepseek", "openai"),
    ("xai", "openai-responses"), ("google-gemini-cli", "google-gemini-cli"),
])
def test_default_threshold_is_eighty_percent_for_every_provider(make_session, provider, api):
    session = make_session(settings={"compaction": {"reserveTokens": 1}})
    session.main.model = Model(provider, "test", api, context_window=1000)
    message = AssistantMessage([TextContent("reply")], usage=Usage(input=799))
    session.main.messages.append(message)
    assert not session.main._compaction_due()
    message.usage.input = 800
    assert session.main._compaction_due()
    session.settings.set("compaction.triggerTokens", 900)
    assert not session.main._compaction_due()
    message.usage.input = 900
    assert session.main._compaction_due()
    session.settings.set("compaction.enabled", False)
    assert not session.main._compaction_due()


@pytest.mark.parametrize("spec", ["openai/test", "openai-codex/test"])
def test_openai_compaction_uses_complete_window_and_roundtrips(make_session, monkeypatch, project, spec):
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 1}})
    session.set_model(spec)
    append(session.main, UserMessage("Original request"), AssistantMessage([TextContent("work " * 200)]), UserMessage("Continue"))
    original = list(session.main.messages)
    requests = []
    result = native_result(session.model)
    payload = [b.data for b in result.content]

    def compact(model, context, options, **kwargs):
        requests.append((model, context, options))
        return copy.deepcopy(result)

    monkeypatch.setattr(compaction, "native_compact", compact)
    monkeypatch.setattr(compaction, "complete", lambda *a, **kw: pytest.fail("must use native compaction"))
    session.compact()
    assert requests[0][0] is session.model
    assert requests[0][1].messages == original
    assert len(session.main.messages) == 1
    assert session.main.messages[0].usage.total == 0
    assert responses.convert_messages(session.main.messages, api=native_api(session.model)) == payload
    assert session.context_tokens() < 1000

    restored = AgentSession(project, model=spec, session=session.store.path, extensions=False)
    try:
        assert responses.convert_messages(restored.main.messages, api=native_api(session.model)) == payload
        monkeypatch.setattr("virgo.ai.auth.authenticated_model", lambda model, signal=None: model)
        observed = []
        reply = AssistantMessage([TextContent("continued")])

        def stream(model, context, options):
            observed.append((model, responses.convert_messages(context.messages, api=model.api)))
            return EventStream(iter([StreamEvent("done", reply)]), reply)

        monkeypatch.setattr(responses if native_api(session.model) == "openai-responses" else importlib.import_module("virgo.ai.providers.codex"), "stream", stream)
        assert ai.stream(restored.model, restored.main.request_context(restored.main.messages)).result().text() == "continued"
        assert observed[0][0].api == native_api(session.model)
        assert observed[0][1] == payload
    finally:
        restored.shutdown()


def test_anthropic_retains_recent_thinking_and_signed_block_on_repeat(make_session, monkeypatch):
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 40}})
    session.set_model("anthropic/claude-opus-5-5")
    recent = AssistantMessage([ThinkingContent("reason", "sig"), TextContent("r" * 100)], api="anthropic")
    append(session.main, UserMessage("old"), AssistantMessage([TextContent("x" * 10000)]), UserMessage("u" * 80), recent)
    requests = []

    def compact(model, context, options, **kwargs):
        requests.append(context)
        return native_result(model)

    monkeypatch.setattr(compaction, "native_compact", compact)
    before_system = session.main.system
    assert session.compact()["kept"] == 2
    assert len(requests[0].messages) == 2
    assert isinstance(session.main.messages[-1].content[0], ThinkingContent)
    assert session.main.system == before_system
    assert session.store.last_compaction()["preserve_thinking"] is True
    append(session.main, UserMessage("next"), AssistantMessage([TextContent("y" * 500)]))
    assert session.compact() is not None
    assert isinstance(requests[1].messages[0].content[0], RawContent)
    assert requests[1].messages[0].content[0].data["signature"] == "signed"


def test_native_failure_preserves_history_and_never_calls_summary(make_session, monkeypatch):
    session = make_session(persist=True)
    session.set_model("openai/test")
    append(session.main, UserMessage("important"), AssistantMessage([TextContent("work")]))
    before = copy.deepcopy(session.main.messages), list(session.main.ids), list(session.store.entries)
    monkeypatch.setattr(compaction, "native_compact", lambda *a, **kw: AssistantMessage(stop_reason="error", error_message="failed"))
    monkeypatch.setattr(compaction, "complete", lambda *a, **kw: pytest.fail("unexpected summary fallback"))
    assert session.compact() is None
    assert (session.main.messages, session.main.ids, session.store.entries) == before


def test_manual_native_compaction_after_abort_starts_new_operation(make_session, monkeypatch):
    session = make_session()
    session.set_model("anthropic/claude-opus-5-5")
    append(session.main, UserMessage("request"), AssistantMessage([TextContent("work")]))
    session.abort()

    def compact(model, context, options, **kwargs):
        assert not options.signal.is_set()
        return native_result(model)

    monkeypatch.setattr(compaction, "native_compact", compact)
    assert session.compact() is not None


def test_provider_switch_restores_history_and_invalidates_small_usage(make_session, monkeypatch):
    session = make_session(persist=True)
    session.set_model("openai/test")
    old = [UserMessage("request"), AssistantMessage([TextContent("x" * 10000)])]
    append(session.main, *old)
    monkeypatch.setattr(compaction, "native_compact", lambda model, *a, **kw: native_result(model))
    session.compact()
    latest = AssistantMessage([TextContent("continued")], usage=Usage(input=10))
    append(session.main, latest)
    session.set_model("faux/test")
    session.main.model.context_window = 1000
    assert session.main.restore_native_context()
    assert session.main.messages == [*old, latest]
    assert session.context_tokens() > 2500
    assert session.main._compaction_due()


def test_in_memory_subagent_can_restore_native_source(make_session, monkeypatch):
    session = make_session()
    runtime = Runtime(session, "helper", "subagent", model=Model("openai", "test", "openai"))
    append(runtime, UserMessage("task"), AssistantMessage([TextContent("work")]))
    original = list(runtime.messages)
    monkeypatch.setattr(compaction, "native_compact", lambda model, *a, **kw: native_result(model))
    runtime.compact()
    assert runtime.restore_native_context(Model("other", "test", "openai"))
    assert runtime.messages == original


def test_foreign_native_state_is_not_silently_dropped():
    state = native_result(Model("openai", "test", "openai"))
    with pytest.raises(ValueError, match="different provider"):
        context_model(Model("other", "test", "openai"), [state])


def test_native_compaction_dispatch_authenticates_before_upgrading_chat(monkeypatch):
    selected = Model("openai", "test", "openai")
    calls = []

    def authenticate(model, signal=None):
        assert model.api == "openai"
        return model

    def compact(model, context, options, **kwargs):
        calls.append(model.api)
        return native_result(model)

    monkeypatch.setattr("virgo.ai.auth.authenticated_model", authenticate)
    monkeypatch.setattr(responses, "compact", compact)
    result = ai.native_compact(selected, ai.Context("SYS", [UserMessage("task")]))
    assert result.stop_reason == "compaction" and calls == ["openai-responses"]
    assert selected.api == "openai"


def test_cut_does_not_split_initial_assistant_tool_call():
    messages = [AssistantMessage([ToolCall("call", "read", {"path": "a"})]), ToolResultMessage("call", "read", [TextContent("data")])]
    assert compaction.find_cut(messages, 100000) == len(messages)


def test_restored_native_thinking_checks_system_and_tools(make_session, monkeypatch, project):
    session = make_session(persist=True, settings={"compaction": {"keepRecentTokens": 40}})
    session.set_model("anthropic/claude-opus-5-5")
    append(session.main, UserMessage("old"), AssistantMessage([TextContent("x" * 10000)]), UserMessage("u" * 80),
           AssistantMessage([ThinkingContent("reason", "sig"), TextContent("r" * 100)], api="anthropic"))
    monkeypatch.setattr(compaction, "native_compact", lambda model, *a, **kw: native_result(model))
    session.compact()
    restored = AgentSession(project, model=session.model.spec, session=session.store.path, permission_mode="accept-all")
    try:
        context = restored.main.request_context(restored.main.messages)
        assert any(isinstance(block, ThinkingContent) for block in context.messages[-1].content)
        restored.main.system += "\nChanged instructions"
        context = restored.main.request_context(restored.main.messages)
        assert not any(isinstance(block, ThinkingContent) for block in context.messages[-1].content)
        assert context.messages[0].content[0].data["signature"] == "signed"
    finally:
        restored.shutdown()
