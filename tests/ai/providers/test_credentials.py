"""Saved credentials reach the actual SDK requests without changing chat/tool history."""

from __future__ import annotations

import threading

import pytest

from virgo.ai.providers import anthropic as anth
from virgo.ai.providers import openai as oai
from virgo.ai.types import (
    AssistantMessage,
    Context,
    Model,
    StreamOptions,
    TextContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

from tests.support.anthropic import ANTHROPIC_EVENTS
from tests.support.http import sse


def test_anthropic_saved_key_and_oauth_use_distinct_auth(server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-key")
    server["body"] = sse(ANTHROPIC_EVENTS, named=True)
    model = Model("anthropic", "claude-test", "anthropic", base_url=server["url"])
    model.options = {"apiKey": "saved-key", "authType": "api_key"}
    assert anth.stream(model, Context("SYS", [UserMessage("hi")]), StreamOptions()).result().stop_reason == "tool_use"
    headers = server["requests"][-1]["headers"]
    assert headers.get("x-api-key") == "saved-key" or headers.get("authorization") == "Bearer saved-key"

    server["body"] = sse(ANTHROPIC_EVENTS, named=True)
    model.options = {"apiKey": "saved-access", "authType": "oauth"}
    ctx = Context(
        "SYS",
        [
            UserMessage("hi"),
            AssistantMessage(content=[ToolCall("old-call", "read", {"path": "old"})]),
            ToolResultMessage("old-call", "read", [TextContent("old contents")]),
        ],
        [ToolSpec("read", "Read a file", {"type": "object"})],
    )
    result = anth.stream(model, ctx, StreamOptions()).result()
    assert result.stop_reason == "tool_use"
    assert result.tool_calls()[0].name == "read"
    request = server["requests"][-1]
    headers = request["headers"]
    assert headers["authorization"] == "Bearer saved-access"
    assert "x-api-key" not in headers
    assert headers["user-agent"].startswith("claude-cli/")
    assert headers["x-app"] == "cli"
    assert all(beta in headers["anthropic-beta"] for beta in anth.BETA_OAUTH)
    assert request["json"]["system"][-1]["text"] == "SYS"
    assert request["json"]["system"][0]["text"].startswith("You are Claude Code")
    # Tool names are sent unchanged for every auth type.
    assert request["json"]["tools"][0]["name"] == "read"
    assert request["json"]["messages"][1]["content"][0]["name"] == "read"
    assert ctx.messages[1].tool_calls()[0].name == "read"


def _completion_body():
    return sse(
        [{"id": "chat-1", "object": "chat.completion.chunk", "created": 1, "model": "test", "choices": [
            {"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}
        ]}],
        named=False,
    )


def test_saved_key_rotation_and_removal_take_effect_next_call(server, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-key")
    server["body"] = _completion_body()
    model = Model("deepseek", "test", "openai", base_url=server["url"], api_key_env="DEEPSEEK_API_KEY")
    for value in ("saved-key", "refreshed-key", None):
        model.options = {"apiKey": value} if value else {}
        msg = oai.stream(model, Context("", [UserMessage("hello")]), StreamOptions()).result()
        assert msg.text() == "OK"
    auth = [request["headers"]["authorization"] for request in server["requests"]]
    assert auth == ["Bearer saved-key", "Bearer refreshed-key", "Bearer environment-key"]


def test_kimi_subscription_bearer_and_header_cache_isolation(server, monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "environment-key")
    server["body"] = _completion_body()
    model = Model("kimi-coding", "kimi-for-coding", "openai", base_url=server["url"], api_key_env="KIMI_API_KEY")
    for header in ("first", "second"):
        model.options = {"apiKey": "kimi-access", "authType": "oauth", "headers": {"X-Test": header}}
        assert oai.stream(model, Context("", [UserMessage("hello")]), StreamOptions()).result().text() == "OK"
    assert [r["headers"]["authorization"] for r in server["requests"]] == ["Bearer kimi-access"] * 2
    assert [r["headers"]["x-test"] for r in server["requests"]] == ["first", "second"]


@pytest.mark.parametrize("adapter,api", [(anth, "anthropic"), (oai, "openai")])
def test_cancelled_request_does_not_send_credentials(server, adapter, api):
    signal = threading.Event()
    signal.set()
    model = Model("test", "test", api, base_url=server["url"], options={"apiKey": "secret"})
    result = adapter.stream(model, Context("", [UserMessage("hi")]), StreamOptions(signal=signal)).result()
    assert result.stop_reason == "aborted"
    assert server["requests"] == []


def test_provider_effort_mapping_omits_unsupported_levels():
    model = Model("kimi", "test", "openai", reasoning=True, options={"reasoningEffortMap": {"medium": None, "max": "max"}})
    context = Context("", [UserMessage("hi")])
    assert "reasoning_effort" not in oai.build_params(model, context, StreamOptions(effort="medium"))
    assert oai.build_params(model, context, StreamOptions(effort="max"))["reasoning_effort"] == "max"


@pytest.mark.parametrize("effort", ["xhigh", "max"])
@pytest.mark.parametrize("declared_by", ["supported", "default", "unknown"])
def test_openai_request_preserves_declared_effort_without_low(effort, declared_by):
    model = Model("openai", "test", "openai", reasoning=True)
    if declared_by == "supported":
        model.supported_reasoning_levels = ("high", effort)
    elif declared_by == "default":
        model.default_reasoning_level = effort
    params = oai.build_params(model, Context("", [UserMessage("hi")]), StreamOptions(effort=effort))
    assert params["reasoning_effort"] == ("high" if declared_by == "unknown" else effort)


def test_explicit_effort_map_overrides_model_metadata():
    model = Model(
        "custom", "test", "openai", reasoning=True,
        supported_reasoning_levels=("low", "xhigh", "max"), default_reasoning_level="max",
        options={"reasoningEffortMap": {"low": None, "xhigh": "high", "max": None}},
    )
    context = Context("", [UserMessage("hi")])
    assert "reasoning_effort" not in oai.build_params(model, context, StreamOptions(effort="low"))
    assert oai.build_params(model, context, StreamOptions(effort="xhigh"))["reasoning_effort"] == "high"
    assert "reasoning_effort" not in oai.build_params(model, context, StreamOptions(effort="max"))


@pytest.mark.parametrize("env", [None, "VIRGO_LOCAL_API_KEY"])
def test_custom_openai_after_logout_never_uses_openai_environment_key(server, monkeypatch, env):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    monkeypatch.delenv("VIRGO_LOCAL_API_KEY", raising=False)
    server["body"] = _completion_body()
    model = Model("local", "test", "openai", base_url=server["url"], api_key_env=env)
    model.options = {"apiKey": "saved-local-key"}
    ctx = Context("", [UserMessage("hello")])
    assert oai.stream(model, ctx, StreamOptions()).result().text() == "OK"
    model.options = {}  # Credential removed by /logout.
    assert oai.stream(model, ctx, StreamOptions()).result().text() == "OK"
    assert [r["headers"]["authorization"] for r in server["requests"]] == [
        "Bearer saved-local-key", "Bearer not-set",
    ]


@pytest.mark.parametrize("env", [None, "VIRGO_CUSTOM_API_KEY"])
def test_custom_anthropic_after_logout_never_uses_sdk_environment_credentials(server, monkeypatch, env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-anthropic-token")
    monkeypatch.delenv("VIRGO_CUSTOM_API_KEY", raising=False)
    server["body"] = sse(ANTHROPIC_EVENTS, named=True)
    model = Model("custom", "test", "anthropic", base_url=server["url"], api_key_env=env)
    model.options = {"apiKey": "saved-custom-key"}
    ctx = Context("", [UserMessage("hello")])
    assert anth.stream(model, ctx, StreamOptions()).result().stop_reason == "tool_use"
    model.options = {}
    result = anth.stream(model, ctx, StreamOptions()).result()
    assert result.stop_reason == "error" and "No API credential" in result.error_message
    assert len(server["requests"]) == 1


@pytest.mark.parametrize("adapter,api", [(anth, "anthropic"), (oai, "openai")])
def test_custom_provider_uses_only_its_explicit_environment_key(server, monkeypatch, adapter, api):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    monkeypatch.setenv("VIRGO_CUSTOM_API_KEY", "custom-environment-key")
    server["body"] = sse(ANTHROPIC_EVENTS, named=True) if api == "anthropic" else _completion_body()
    model = Model("custom", "test", api, base_url=server["url"], api_key_env="VIRGO_CUSTOM_API_KEY")
    result = adapter.stream(model, Context("", [UserMessage("hello")]), StreamOptions()).result()
    assert result.stop_reason != "error"
    headers = server["requests"][0]["headers"]
    assert headers.get("authorization") == "Bearer custom-environment-key" or headers.get("x-api-key") == "custom-environment-key"
