"""Reactive compaction driven by real provider adapters and scripted HTTP responses."""

import json

import pytest

from virgo.ai.providers import faux
from virgo.harness.store import SessionStore

from tests.support.http import bypass_proxies, fake_api
from tests.support.sdk_streams import POST_PATH, PROVIDERS, api_name, text_reply


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "exchanges, window",
    [
        pytest.param(("overflow", "success"), 64_000, id="prune-only"),
        pytest.param(("overflow", "overflow", "summary", "success"), 64_000, id="prune-retry-overflow"),
        pytest.param(("overflow", "summary", "success"), 2_048, id="still-over-budget-after-prune"),
        pytest.param(("overflow", "overflow", "summary", "overflow"), 64_000, id="bounded-after-summary"),
    ],
)
def test_provider_overflow_recovery(make_session, project, monkeypatch, provider, exchanges, window):
    bypass_proxies(monkeypatch)
    overflow_message = "prompt is too long: 90000 tokens exceeds the maximum context length"
    error = {"message": overflow_message, "type": "invalid_request_error"}
    if provider == "anthropic":
        body = {"type": "error", "error": error}
    else:
        body = {"error": {**error, "code": "context_length_exceeded"}}
    overflow = {"status": 400, "body": json.dumps(body).encode()}
    summary = "CHECKPOINT: preserve the findings and continue."
    recovered = "The original turn recovered."
    replies = {
        "overflow": overflow,
        "summary": {"body": b"".join(text_reply(provider, summary))},
        "success": {"body": b"".join(text_reply(provider, recovered))},
    }
    marker = "ORIGINAL_LARGE_TOOL_RESULT"
    (project / "history.txt").write_text(f"{marker}\n" * 500, encoding="utf-8")
    long_user_history = "Retain this user history. " * 500

    with fake_api(replies=[replies[exchange] for exchange in exchanges]) as server:
        session = make_session(
            faux.reply(tools=[("read", {"path": "history.txt"}, "historical-read")]),
            faux.reply("Inspected the file."),
            persist=True,
            refresh_models=False,
            settings={
                "compaction": {"enabled": False, "keepRecentTokens": 0},
                "providers": {"local": {
                    "api": api_name(provider), "baseUrl": server["url"], "apiKey": "local-test-key",
                    "models": {"test": {"contextWindow": window, "reasoning": False}},
                }},
            },
        )
        session.prompt(long_user_history)
        session.set_model("local/test")
        session.prompt("Finish the original task.")

    requests = server["requests"]
    assert [req["path"] for req in requests] == [POST_PATH[provider]] * len(exchanges)
    payloads = [json.dumps(req["json"]) for req in requests]
    assert marker in payloads[0]
    assert all(marker not in payload for payload in payloads[1:])
    assert ["context summarization assistant" in payload for payload in payloads] == [
        exchange == "summary" for exchange in exchanges
    ]
    assert "Pruned tool result" in payloads[1]
    if "summary" in exchanges:
        assert summary in payloads[-1]
    if exchanges[-1] == "success":
        assert session.main.agent.last_text == recovered
    else:
        assert session.main.messages[-1].error_kind == "overflow"

    store = SessionStore.load(session.store.path)
    compactions = [entry for entry in store.branch() if entry["type"] == "compaction"]
    expected = [("prune", "overflow", None)]
    if "summary" in exchanges:
        expected.append(("summary", "overflow", "serialized"))
    assert [(e["mode"], e["reason"], e.get("strategy")) for e in compactions] == expected
