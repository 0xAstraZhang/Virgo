"""Text-only SDK wire replies shared by provider, print-mode and fullscreen tests."""

from __future__ import annotations

import json
import sys

from tests.support.anthropic import ANTHROPIC_EVENTS
from tests.support.http import sse

PROVIDERS = ("openai", "anthropic", "responses")
POST_PATH = {"openai": "/chat/completions", "anthropic": "/v1/messages", "responses": "/responses"}
PARTIAL = "First delta"
RECOVERED = "RECOVERED_AFTER_HANG_OK"


def api_name(provider):
    return "openai-responses" if provider == "responses" else provider


def text_reply(provider, text, *, count=1):
    """Return separately writable SSE bytes through the text delta(s) and through completion."""
    if provider == "openai":
        chunk = {"id": "chat-1", "object": "chat.completion.chunk", "created": 1, "model": "test"}
        choice = {"index": 0, "delta": {"content": text}, "finish_reason": None}
        prefix = sse([{**chunk, "choices": [choice]}] * count, done=False)
        return prefix, sse([
            {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**chunk, "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}},
        ])
    if provider == "anthropic":
        delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
        prefix = sse([
            ANTHROPIC_EVENTS[0],
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            *([delta] * count),
        ], named=True)
        return prefix, sse([
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ], named=True)
    assert provider == "responses"
    item = {"type": "message", "id": "msg_1", "role": "assistant", "content": []}
    delta = {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
             "item_id": "msg_1", "delta": text}
    prefix = sse([
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        *([delta] * count),
    ], named=True)
    return prefix, sse([{"type": "response.completed", "response": {
        "id": "resp_1", "status": "completed",
        "output": [{**item, "content": [{"type": "output_text", "text": text * count, "annotations": []}]}],
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }}], named=True)


def cli_args(home, provider, url):
    home.mkdir(exist_ok=True)
    settings = {
        "language": "en",
        "providers": {"local": {"api": api_name(provider), "apiKey": "local-test-key",
                                "baseUrl": url, "models": {"test": {"reasoning": False}}}},
        "plugins": {"web": {"enabled": False}},
    }
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return [sys.executable, "-m", "virgo", "--model", "local/test", "--no-session",
            "--no-extensions", "--permission", "no-limit"]
