"""Read-only accounting for the current context, not cumulative token usage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import compaction

if TYPE_CHECKING:
    from ..ai.types import Model, ToolSpec
    from .session import Runtime

CATEGORIES = (
    "System prompt",
    "System tools",
    "MCP tools",
    "MCP server instruction",
    "Memory files",
    "Skills",
    "Messages",
)


@dataclass(frozen=True)
class ContextRow:
    name: str
    tokens: int


@dataclass(frozen=True)
class ContextReport:
    model: str
    rows: tuple[ContextRow, ...]
    used: int
    window: int
    trigger: int
    enabled: bool
    free: int
    buffer: int
    over_threshold: bool
    reported_usage: bool


def compaction_threshold(rt: Runtime) -> tuple[bool, int]:
    settings = rt.session.settings
    enabled = bool(rt.compaction and settings.get("compaction.enabled", True))
    trigger = settings.get("compaction.triggerTokens") or (
        rt.model.context_window - int(settings.get("compaction.reserveTokens", 16384))
    )
    return enabled, int(trigger)


def context_signature(model: Model, system: str, tools: list[ToolSpec]) -> tuple:
    return model.spec, system, tuple(_tool_text(tool) for tool in tools)


def _tool_text(tool: ToolSpec) -> str:
    return json.dumps(
        {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
        ensure_ascii=False, sort_keys=True,
    )


def _fixed_weights(rt: Runtime) -> tuple[dict[str, int], tuple]:
    """Estimate only the frozen system text and currently offered tool schemas.

    Do not rebuild the prompt or invoke extension context hooks during inspection.
    Skills and memories read into the conversation remain under Messages.
    """
    chars = dict.fromkeys(CATEGORIES[:-1], 0)
    system, tools = rt.request_system_and_tools()
    for part in rt.system_parts:
        category = part.category if part.category in chars else "System prompt"
        chars[category] += len(part.text)
    # Separators and the live permission-mode instruction belong to the system prompt.
    chars["System prompt"] += max(0, len(system) - sum(len(p.text) for p in rt.system_parts))
    for tool in tools:
        category = "MCP tools" if tool.name.startswith("mcp__") else "System tools"
        chars[category] += len(_tool_text(tool))
    return {name: (size + 3) // 4 for name, size in chars.items()}, context_signature(rt.model, system, tools)


def _valid_from(rt: Runtime, signature: tuple, message_count: int) -> int:
    if signature != rt.usage_context_signature:
        return message_count
    return max(rt.compacted_at, rt.usage_valid_from)


def estimate_context_tokens(rt: Runtime) -> int:
    messages = list(rt.messages)
    weights, signature = _fixed_weights(rt)
    return compaction.estimate_tokens(
        messages, _valid_from(rt, signature, len(messages)), fixed_tokens=sum(weights.values())
    )


def _allocate(weights: dict[str, int], total: int) -> dict[str, int]:
    """Fit estimated category proportions to reported usage without losing tokens."""
    size = sum(weights.values())
    if not size:
        return {name: total if name == "Messages" else 0 for name in weights}
    result = {name: weight * total // size for name, weight in weights.items()}
    remainder = total - sum(result.values())
    order = sorted(weights, key=lambda name: weights[name] * total % size, reverse=True)
    for name in order[:remainder]:
        result[name] += 1
    return result


def build_context_report(rt: Runtime) -> ContextReport:
    messages = list(rt.messages)
    weights, signature = _fixed_weights(rt)
    valid_from = _valid_from(rt, signature, len(messages))
    fixed = sum(weights.values())
    weights["Messages"] = sum(compaction._chars(message) for message in messages) // 4
    used = compaction.estimate_tokens(messages, valid_from, fixed_tokens=fixed)
    reported = compaction.last_usage_index(messages, valid_from) is not None
    allocations = _allocate(weights, used)
    window = max(0, rt.model.context_window)
    enabled, trigger = compaction_threshold(rt)
    remaining = max(0, window - used)
    buffer = min(remaining, max(0, window - trigger)) if enabled else 0
    free = remaining - buffer
    rows = tuple(ContextRow(name, allocations[name]) for name in CATEGORIES) + (
        ContextRow("Free space", free),
        ContextRow("Autocompact buffer", buffer),
    )
    return ContextReport(
        model=rt.model.spec, rows=rows, used=used, window=window, trigger=trigger,
        enabled=enabled, free=free, buffer=buffer,
        over_threshold=enabled and used > trigger, reported_usage=reported,
    )


def render_context_report(report: ContextReport) -> str:
    denominator = max(1, report.window)
    lines = [
        f"Context · {report.model}",
        f"~{report.used:,} / {report.window:,} tokens ({report.used / denominator:.1%})",
        "",
        f"{'Category':<24} {'~Tokens':>12} {'Window':>8}",
    ]
    for row in report.rows:
        lines.append(f"{row.name:<24} {row.tokens:>12,} {row.tokens / denominator:>8.1%}")
    lines.append("")
    status = "on" if report.enabled else "off"
    lines.append(f"Auto-compact: {status}")
    lines.append(f"Threshold: > {report.trigger:,} tokens ({report.trigger / denominator:.1%} of window)")
    if report.enabled:
        reserved = min(report.window, max(0, report.window - report.trigger))
        lines.append(f"Reserved buffer: {reserved:,} tokens; remaining: {report.buffer:,}")
        if report.over_threshold:
            lines.append("Threshold exceeded; auto-compact runs before the next model call.")
        else:
            lines.append(f"Until threshold: ~{max(0, report.trigger - report.used):,} tokens")
    if report.used > report.window:
        lines.append(f"Context exceeds the window by ~{report.used - report.window:,} tokens.")
    lines.append("")
    if report.reported_usage:
        lines.append("Total: last provider usage + estimated new messages (cache included).")
        lines.append("Categories: character estimates scaled to that total.")
    else:
        lines.append("Estimated from characters (~4 per token), including system and tools.")
    lines.append("Memory files: loaded instructions and memory index; Skills: loaded catalog.")
    lines.append("Messages includes tool results, loaded skill/memory bodies and summaries.")
    return "\n".join(lines)
