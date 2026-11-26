"""Shared compaction record codec and context replay rules.

Placed below both ``harness.store`` and ``harness.compaction`` so persistence
and compaction share one schema without circular dependencies.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from ..ai.transform import strip_thinking
from ..ai.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolResultMessage,
    Usage,
    UserMessage,
    message_from_dict,
)

_GRANT_KEYS = ("read_paths", "write_paths", "network_allowed", "unrestricted", "web_allowed")

SUMMARY_PREFIX = (
    "<conversation_summary>\nThis checkpoint summarizes only the earlier evicted turns before the retained recent messages. "
    "Newer retained messages below supersede earlier status claims in this summary. "
    "Use this as reference material. Continue any unfinished turn without repeating completed steps.\n\n"
)


def summary_message(text: str, *, timestamp: float | None = None) -> UserMessage:
    return UserMessage(
        f"{SUMMARY_PREFIX}{text.strip()}\n</conversation_summary>",
        kind="summary",
        timestamp=time.time() if timestamp is None else timestamp,
    )


def stored_details(details, *, transcript: bool):
    if not isinstance(details, dict) or not isinstance(boundary := details.get("boundary"), dict):
        return details
    compact = boundary.copy()
    for key in (
        "requested", "runtime_read_dependencies", "ambient_read_access", "unmanaged",
        "read_deny_paths", "write_deny_paths",
    ):
        compact.pop(key, None)
    if transcript:
        compact.pop("effective", None)
    elif isinstance(effective := boundary.get("effective"), dict):
        for source in ("native_process", "file_tools"):
            if isinstance(policy := effective.get(source), dict):
                compact["effective"] = {source: {key: policy[key] for key in _GRANT_KEYS if key in policy}}
                break
    return {**details, "boundary": compact}


def compacted_message(message: Message) -> Message:
    """Use the same retained metadata live and on disk. Usage measured the old window."""
    if isinstance(message, AssistantMessage):
        return replace(message, usage=Usage())
    if isinstance(message, ToolResultMessage):
        details = stored_details(message.details, transcript=False)
        if details is not message.details:
            return replace(message, details=details)
    return message


def validate_compaction_record(record: dict) -> None:
    if "context" in record:
        raise ValueError("legacy native compaction records are no longer supported; start a new session")
    mode = record.get("mode", "summary")
    if mode == "prune":
        pruned = record.get("pruned")
        if not isinstance(pruned, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in pruned.items()
        ):
            raise ValueError("invalid compaction pruned map")
    elif mode == "summary":
        if not isinstance(record.get("summary"), str):
            raise ValueError("missing compaction summary")
        kept = record.get("kept_ids", [])
        if not isinstance(kept, list) or not all(isinstance(key, str) for key in kept):
            raise ValueError("invalid compaction kept_ids")
    else:
        raise ValueError(f"unknown compaction mode {mode!r}")


def replay_compaction_context(
    path: list[dict],
    by_id: dict[str, dict],
    *,
    created_at: Callable[[dict, float], float],
) -> tuple[list, list]:
    """Rebuild model-visible messages and entry IDs from a session branch path."""
    comp_i = max(
        (i for i, e in enumerate(path) if e["type"] == "compaction" and e.get("mode", "summary") == "summary"),
        default=None,
    )
    pruned_map: dict[str, str] = {}
    for entry in path:
        if entry["type"] == "compaction" and entry.get("mode") == "prune":
            pruned_map.update(entry.get("pruned", {}))

    def maybe_prune(msg: Message, entry_id: str) -> Message:
        if isinstance(msg, ToolResultMessage) and entry_id in pruned_map:
            return compacted_message(replace(msg, content=[TextContent(pruned_map[entry_id])]))
        return msg

    if comp_i is None:
        pairs = [
            (message_from_dict(e["message"]), e["id"])
            for e in path
            if e["type"] == "message"
        ]
        messages = [m for m, _ in pairs]
        ids = [i for _, i in pairs]
        if not pruned_map:
            return messages, ids
        return [maybe_prune(m, eid) for m, eid in zip(messages, ids, strict=True)], ids

    comp = path[comp_i]
    timestamp = comp.get("summary_timestamp", created_at(comp, 0.0))
    messages = [summary_message(comp["summary"], timestamp=timestamp)]
    ids = [comp["id"]]
    for entry_id in comp.get("kept_ids", []):
        entry = by_id.get(entry_id)
        if not entry or entry["type"] != "message":
            continue
        for msg in strip_thinking([message_from_dict(entry["message"])]):
            messages.append(maybe_prune(compacted_message(msg), entry_id))
            ids.append(entry_id)
    for entry in path[comp_i + 1 :]:
        if entry["type"] == "message":
            messages.append(maybe_prune(message_from_dict(entry["message"]), entry["id"]))
            ids.append(entry["id"])
    return messages, ids
