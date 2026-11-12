"""Verify auxiliary defaults against outgoing provider controls, without a live API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from virgo import ai
from virgo.ai.providers import anthropic, codex, openai, responses
from virgo.ai.types import AssistantMessage, Context, Model, StreamOptions, TextContent


@pytest.mark.parametrize("spec,adapter,limit_field", [
    ("deepseek/deepseek-flash", openai, "max_tokens"),
    ("anthropic/claude-haiku-5-5", anthropic, "max_tokens"),
    ("xai/grok-4.3", responses, "max_output_tokens"),
])
def test_explicit_total_limit_reaches_provider_without_headroom(monkeypatch, spec, adapter, limit_field):
    model = ai.resolve_model(spec)
    sent = []

    def capture(selected, context, options):
        sent.append(adapter.build_params(selected, context, options))
        return SimpleNamespace(result=lambda: AssistantMessage([TextContent("ok")]))

    monkeypatch.setattr(ai, "stream", capture)
    reply = ai.complete(model, "Review action", "public download", effort="low", total_max_tokens=4096)
    assert sent[0][limit_field] == 4096
    effort = sent[0].get("reasoning_effort") or sent[0].get("output_config", {}).get("effort")
    effort = effort or sent[0].get("reasoning", {}).get("effort")
    assert effort == "low"
    assert reply.request_metadata["max_tokens"] == 4096
    assert reply.request_metadata["effective_effort"] == effort
    assert reply.request_metadata["token_limit_supported"] is True


def test_explicit_total_limit_respects_smaller_model_capacity(monkeypatch):
    model = Model("faux", "small", "faux", max_tokens=2048)
    sent = []

    def capture(_model, _context, options):
        sent.append(options.max_tokens)
        return SimpleNamespace(result=lambda: AssistantMessage())

    monkeypatch.setattr(ai, "stream", capture)
    reply = ai.complete(model, "", "", total_max_tokens=4096)
    assert sent == [2048]
    assert reply.request_metadata["requested_max_tokens"] == 4096
    assert reply.request_metadata["max_tokens"] == 2048


@pytest.mark.parametrize("budgets", [
    {"max_tokens": 512, "total_max_tokens": 4096},
    {"total_max_tokens": 0}, {"total_max_tokens": -1},
    {"total_max_tokens": True}, {"total_max_tokens": 4096.0},
])
def test_invalid_or_ambiguous_total_limit_never_sends_a_request(monkeypatch, budgets):
    monkeypatch.setattr(ai, "stream", lambda *_: pytest.fail("invalid budget reached provider"))
    with pytest.raises(ValueError):
        ai.complete(ai.resolve_model("faux/small"), "", "", **budgets)


def test_account_adapter_audit_does_not_claim_an_unsent_token_limit():
    model = ai.resolve_model("openai-codex/gpt-6-luna")
    sent = codex.build_params(model, Context("", []), StreamOptions(effort="low", max_tokens=4096))
    assert "max_tokens" not in sent and "max_output_tokens" not in sent
    metadata = ai.completion_request_metadata(model, "low", 4096)
    assert metadata["requested_max_tokens"] == 4096
    assert metadata["max_tokens"] is None
    assert metadata["token_limit_supported"] is False
    assert metadata["token_limit_omission_reason"]


def test_auxiliary_audit_tracks_low_fallback_and_explicit_budget(make_session, monkeypatch):
    session = make_session()
    session.small_model = Model(
        "custom", "small", "openai", reasoning=True,
        supported_reasoning_levels=("high",), default_reasoning_level="high",
    )
    sent = []

    def capture(model, context, options):
        sent.append(openai.build_params(model, context, options))
        return SimpleNamespace(result=lambda: AssistantMessage())

    monkeypatch.setattr(ai, "stream", capture)
    session.complete("permission", "", "", effort=session.small_model_effort, total_max_tokens=4096)
    record = session.store.custom("model_call")[0]
    assert sent[0]["max_tokens"] == record["max_tokens"] == 4096
    assert sent[0]["reasoning_effort"] == record["effective_effort"] == "high"
    assert record["requested_effort"] == "low"
    assert record["effort_fallback_reason"] == "low is unavailable; using the declared model default"


def test_auxiliary_call_can_keep_original_accounting_store_after_switch(make_session, monkeypatch):
    session = make_session()
    original = session.store
    session.new_session()
    monkeypatch.setattr(ai, "complete", lambda *a, **kw: AssistantMessage())
    session.complete("permission", "", "", total_max_tokens=4096, accounting_store=original,
                     call_metadata={"evidence_ids": ["original-message"], "truncated": False})
    assert session.store.custom("model_call") == []
    record = original.custom("model_call")[0]
    assert record["review_context"] == {"evidence_ids": ["original-message"], "truncated": False}
    assert record["max_tokens"] == 4096


def test_omitted_auxiliary_effort_uses_low_but_explicit_none_is_preserved(make_session, monkeypatch):
    session = make_session()
    session.small_model = ai.resolve_model("deepseek/deepseek-flash")
    efforts = []

    def complete(*args, **kwargs):
        efforts.append(kwargs["effort"])
        return AssistantMessage()

    monkeypatch.setattr(ai, "complete", complete)
    session.complete("permission", "", "")
    session.complete("permission", "", "", effort=None)
    assert efforts == ["low", None]


@pytest.mark.parametrize("requested,options,expected", [
    ("low", {}, "low"),
    ("max", {}, "high"),
    ("medium", {"reasoningEffortMap": {"medium": None}}, None),
    ("low", {"reasoningEffortMap": {"low": "low"}}, "low"),
])
def test_audit_effective_effort_matches_compatible_adapter(requested, options, expected):
    model = Model("custom", "small", "openai", reasoning=True, options=options)
    params = openai.build_params(model, Context("", []), StreamOptions(effort=requested, max_tokens=4096))
    metadata = ai.completion_request_metadata(model, requested, 4096)
    assert params.get("reasoning_effort") == metadata["effective_effort"] == expected
