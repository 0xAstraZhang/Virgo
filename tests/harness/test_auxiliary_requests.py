"""Verify auxiliary defaults against outgoing provider controls, without a live API."""

from __future__ import annotations

import pytest

from virgo import ai
from virgo.ai.providers import anthropic, codex, openai, responses
from virgo.ai.types import AssistantMessage, Context, Model, StreamOptions


def test_account_adapter_audit_does_not_claim_an_unsent_token_limit():
    model = ai.resolve_model("openai-codex/gpt-6-luna")
    sent = codex.build_params(model, Context("", []), StreamOptions(effort="low", max_tokens=4096))
    assert "max_tokens" not in sent and "max_output_tokens" not in sent
    metadata = ai.completion_request_metadata(model, "low", 4096)
    assert metadata["requested_max_tokens"] == 4096
    assert metadata["max_tokens"] is None
    assert metadata["token_limit_supported"] is False
    assert metadata["token_limit_omission_reason"]


def test_auxiliary_call_can_keep_original_accounting_store_after_switch(make_session, monkeypatch):
    session = make_session()
    original = session.store
    session.new_session()
    monkeypatch.setattr(ai, "complete", lambda *a, **kw: AssistantMessage())
    session.complete("compaction", "", "", max_tokens=1024, accounting_store=original)
    assert session.store.custom("model_call") == []
    record = original.custom("model_call")[0]
    assert record["purpose"] == "compaction"
    assert record["max_tokens"] >= 1024


def test_omitted_auxiliary_effort_uses_low_but_explicit_none_is_preserved(make_session, monkeypatch):
    session = make_session()
    session.small_model = ai.resolve_model("deepseek/deepseek-flash")
    efforts = []

    def complete(*args, **kwargs):
        efforts.append(kwargs["effort"])
        return AssistantMessage()

    monkeypatch.setattr(ai, "complete", complete)
    session.complete("compaction", "", "")
    session.complete("compaction", "", "", effort=None)
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


@pytest.mark.parametrize("spec,requested,builder,field,wire,effective", [
    ("deepseek/deepseek-flash", "medium", openai.build_params, "reasoning_effort", None, "high"),
    ("kimi/kimi-k3.1-pro", "medium", openai.build_params, "reasoning_effort", None, None),
    ("deepseek/deepseek-flash", None, openai.build_params, "reasoning_effort", None, None),
    ("deepseek/deepseek-chat", "high", openai.build_params, "reasoning_effort", None, None),
    ("openai-codex/gpt-6.1-terra", "max", codex.build_params, "reasoning", "xhigh", "xhigh"),
    ("xai/grok-4.3", "max", responses.build_params, "reasoning", "xhigh", "xhigh"),
    ("anthropic/claude-opus-5-5", "max", anthropic.build_params, "output_config", "max", "max"),
])
def test_resolved_effort_agrees_with_request_and_wire_metadata(spec, requested, builder, field, wire, effective):
    model = ai.resolve_model(spec)
    params = builder(model, Context("", []), StreamOptions(effort=requested))
    sent = params.get(field)
    assert (sent.get("effort") if isinstance(sent, dict) else sent) == wire
    assert ai.completion_request_metadata(model, requested, None)["effective_effort"] == wire
    result = ai.resolved_effort(model, requested)
    assert result.wire == wire and result.effective == effective
