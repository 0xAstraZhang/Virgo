from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path

from virgo.ai import resolve_model
from virgo.ai.providers import anthropic as anth
from virgo.ai.providers import faux
from virgo.ai.providers import openai as oai
from virgo.ai.transform import normalize
from virgo.ai.types import (
    AssistantMessage,
    Context,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)
from virgo.features.mcp import MCPClient

from .conftest import RecordingUI, calls

SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def test_mcp_client_handshake_and_calls(tmp_path):
    handshake = tmp_path / "initialize.json"
    c = MCPClient("echo", sys.executable, [str(SERVER), str(handshake)])
    c.connect(timeout=10)
    try:
        assert json.loads(handshake.read_text())["clientInfo"] == {
            "name": "virgo",
            "version": version("virgo"),
        }
        assert [t["name"] for t in c.tools] == ["echo", "add"]
        assert c.call("add", {"a": 2, "b": 3})["content"][0]["text"] == "5"
    finally:
        c.close()


def test_mcp_tools_are_namespaced_and_permission_checked(make_session, project, monkeypatch):
    reviewed = []

    def review(_session, event, _reason):
        reviewed.append(event.tool_name)
        return False, "external service needs approval"

    monkeypatch.setattr("virgo.features.permission.review_tool_call", review)
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"echo": {"command": sys.executable, "args": [str(SERVER)]}}})
    )
    ui = RecordingUI(answers=["y"])
    s = make_session(
        faux.reply(tools=[("mcp__echo__echo", {"text": "hello mcp"}), ("mcp__echo__add", {"a": 1, "b": 2})]),
        faux.reply("ok"),
        ui=ui,
        mode="auto-review",
        settings={"permissions": {"allow": ["mcp__echo__add"]}},
    )
    s.prompt("use mcp")
    assert "mcp__echo__echo" in calls()[0]["tools"]
    res = [m.text() for m in s.main.messages if isinstance(m, ToolResultMessage)]
    assert res == ["hello mcp", "3"]
    assert len(ui.questions) == 1  # echo asked; add allowed by rule
    assert reviewed == ["mcp__echo__echo"]
    assert s.main.agent.last_text == "ok"


def test_mcp_reconnect_rebinds_tools_to_the_new_connection(make_session, project):
    (project / ".virgo").mkdir()
    config = {"mcpServers": {"echo": {"command": sys.executable, "args": [str(SERVER)]}}}
    (project / ".virgo" / "mcp.json").write_text(json.dumps(config))
    ui = RecordingUI()
    s = make_session(
        faux.reply(tools=[("mcp__echo__add", {"a": 1, "b": 2})]),
        faux.reply("first"),
        faux.reply(tools=[("mcp__echo__add", {"a": 20, "b": 30})]),
        faux.reply("second"),
        ui=ui,
    )
    s.prompt("add")
    first = s.services["mcp"]["echo"]
    s.prompt("/mcp reconnect")
    assert s.services["mcp"]["echo"] is not first and "duplicate" not in ui.text
    s.prompt("add again")
    # The tools used to stay bound to the closed client: "ValueError: write to closed file".
    results = [(m.text(), m.is_error) for m in s.main.messages if isinstance(m, ToolResultMessage)]
    assert results == [("3", False), ("50", False)]
    assert [t.name for t in s.main.tools].count("mcp__echo__add") == 1

    # A server removed from the configuration takes its tools with it.
    (project / ".virgo" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    s.prompt("/mcp reconnect")
    assert not any(t.name.startswith("mcp__") for t in s.main.tools)
    assert s.tool("mcp__echo__add", "subagent") is None


def test_anthropic_request_shape():
    model = resolve_model("anthropic/claude-opus-5-5")
    msgs = [
        UserMessage("hi"),
        AssistantMessage(
            [
                ThinkingContent("hmm", signature="sig"),
                TextContent("let me look"),
                ToolCall("t1", "read", {"path": "a"}),
            ],
            api="anthropic",
        ),
        ToolResultMessage("t1", "read", [TextContent("content")]),
        UserMessage("<notification>done</notification>", kind="notification"),
    ]
    ctx = Context("SYS", normalize(msgs, model), [ToolSpec("read", "Read", {"type": "object"})])
    p = anth.build_params(model, ctx, StreamOptions(effort="high"))
    assert p["thinking"]["type"] == "adaptive" and p["output_config"] == {"effort": "high"}
    assert p["cache_control"] == {"type": "ephemeral"} and p["fallbacks"] == "default"
    assert set(p["betas"]) == {anth.BETA_FALLBACK, anth.BETA_BINDING}
    assert p["tools"][0]["eager_input_streaming"] is True
    roles = [m["role"] for m in p["messages"]]
    assert roles == ["user", "assistant", "user"]  # tool result + notification merged into one user turn
    assert p["messages"][1]["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    assert p["messages"][2]["content"][0]["type"] == "tool_result"
    assert "budget_tokens" not in json.dumps(p) and "temperature" not in p


def test_cross_provider_normalization_drops_foreign_thinking_and_orphans():
    model = resolve_model("openai/gpt-5")
    msgs = [
        UserMessage("hi"),
        AssistantMessage([ThinkingContent("x", signature="s"), ToolCall("c1", "read", {})], api="anthropic"),
        UserMessage("interrupting"),
    ]
    out = normalize(msgs, model)
    assert not any(isinstance(b, ThinkingContent) for b in out[1].content)
    assert isinstance(out[2], ToolResultMessage) and out[2].is_error  # synthesized result for the orphan call
    conv = oai.convert_messages("SYS", out)
    assert [m["role"] for m in conv] == ["system", "user", "assistant", "tool", "user"]
    assert conv[2]["tool_calls"][0]["function"]["name"] == "read"


def test_chat_completions_skips_assistant_turns_without_content_or_tool_calls():
    # The output budget ran out while the model was still reasoning: nothing in the turn can be
    # replayed, and sending it as `content: null` makes every later request fail with HTTP 400.
    model = resolve_model("deepseek/deepseek-flash")
    msgs = [
        UserMessage("prove it"),
        AssistantMessage([ThinkingContent("let me think")], api="openai", provider="deepseek", stop_reason="length"),
        UserMessage("never mind"),
    ]
    conv = oai.convert_messages("", normalize(msgs, model), send_reasoning=True)
    assert conv == [{"role": "user", "content": "prove it"}, {"role": "user", "content": "never mind"}]


def test_chat_completions_reasoning_field_accompanies_every_tool_call_turn():
    model = resolve_model("deepseek/deepseek-flash")
    msgs = [
        UserMessage("read it"),
        # Thinking from another provider is dropped, but thinking-mode endpoints still require the field.
        AssistantMessage([ThinkingContent("x", signature="s"), ToolCall("c1", "read", {})], api="anthropic"),
        ToolResultMessage("c1", "read", [TextContent("data")]),
        AssistantMessage(
            [ThinkingContent("check"), ToolCall("c2", "read", {})], api="openai", provider="deepseek"
        ),
        ToolResultMessage("c2", "read", [TextContent("more")]),
        AssistantMessage([TextContent("done")], api="openai", provider="deepseek"),
    ]
    normalized = normalize(msgs, model)
    conv = oai.convert_messages("", normalized, send_reasoning=True)
    assert [m.get("reasoning_content") for m in conv] == [None, "", None, "check", None, None]
    assert "reasoning_content" not in conv[5]
    assert all("reasoning_content" not in m for m in oai.convert_messages("", normalized))


def test_model_resolution_and_custom_providers():
    m = resolve_model("claude-sonnet-5-5")
    assert m.provider == "anthropic" and m.reasoning and m.context_window == 1_000_000
    d = resolve_model(
        "deepseek/deepseek-chat",
        {
            "deepseek": {
                "api": "openai",
                "baseUrl": "https://api.deepseek.com",
                "apiKeyEnv": "DEEPSEEK_API_KEY",
                "models": {"deepseek-chat": {"contextWindow": 64000}},
            }
        },
    )
    assert d.api == "openai" and d.base_url == "https://api.deepseek.com" and d.context_window == 64000


def test_error_classification():
    class E(Exception):
        def __init__(self, status, message):
            self.status_code, self.message = status, message

    assert (
        anth.classify_error(E(400, "prompt is too long: 1200000 tokens > 1000000 maximum"))[0] == "overflow"
    )
    assert anth.classify_error(E(529, "Overloaded"))[0] == "retryable"
    assert anth.classify_error(E(401, "invalid x-api-key"))[0] == "fatal"
