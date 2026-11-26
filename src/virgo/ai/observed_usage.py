"""Provider-reported counts, kept separate from conversation token estimates."""
from __future__ import annotations

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")


def token(value):
    return value if type(value) is int and value >= 0 else None


def empty_usage() -> dict:
    return dict.fromkeys(TOKEN_FIELDS)


def responses_usage(value: dict) -> dict:
    input_details = value.get("input_tokens_details") or {}
    output_details = value.get("output_tokens_details") or {}
    return dict(input_tokens=token(value.get("input_tokens")),
                cached_input_tokens=token(input_details.get("cached_tokens")),
                cache_write_tokens=token(input_details.get("cache_write_tokens")),
                output_tokens=token(value.get("output_tokens")),
                reasoning_tokens=token(output_details.get("reasoning_tokens")))


def chat_usage(value) -> dict:
    prompt_details = getattr(value, "prompt_tokens_details", None)
    completion_details = getattr(value, "completion_tokens_details", None)
    cached = token(getattr(prompt_details, "cached_tokens", None))
    if cached is None:
        cached = token(getattr(value, "prompt_cache_hit_tokens", None))
    return dict(input_tokens=token(getattr(value, "prompt_tokens", None)),
                cached_input_tokens=cached, cache_write_tokens=None,
                output_tokens=token(getattr(value, "completion_tokens", None)),
                reasoning_tokens=token(getattr(completion_details, "reasoning_tokens", None)))


def anthropic_usage(parts: dict) -> dict:
    uncached = token(parts.get("input_tokens"))
    read = token(parts.get("cache_read_input_tokens"))
    write = token(parts.get("cache_creation_input_tokens"))
    counts = (uncached, read, write)
    return dict(input_tokens=sum(counts) if all(n is not None for n in counts) else None,
                cached_input_tokens=read, cache_write_tokens=write,
                output_tokens=token(parts.get("output_tokens")), reasoning_tokens=None)
