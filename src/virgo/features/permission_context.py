"""Public, branch-bound evidence for permission reviews.

This module selects and bounds evidence; it does not infer user authorization.
Only original user messages and recorded human decisions carry that provenance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from ..ai.types import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
)

REVIEW_INPUT_LIMIT = 40_000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _message_token(message) -> str:
    token = getattr(message, "_review_message_token", None)
    if token is None:
        token = uuid.uuid4().hex
        message._review_message_token = token
    return token


def _public_message(message, entry_id: str, agent: str, *, delegated: bool = False) -> dict | None:
    record: dict = {"id": entry_id, "agent": agent}
    if isinstance(message, UserMessage):
        record["role"] = (
            "context" if message.kind == "summary" else "notification" if message.kind
            else "delegation" if delegated else "user"
        )
        record["text"] = message.text()
        if message.kind:
            record["kind"] = message.kind
    elif isinstance(message, AssistantMessage):
        record["role"] = "assistant"
        # Do not reuse strip_thinking: RawContent can contain encrypted reasoning.
        record["text"] = "".join(b.text for b in message.content if isinstance(b, TextContent))
        calls = [
            {"id": b.id, "name": b.name, "arguments": copy.deepcopy(b.arguments)}
            for b in message.content if isinstance(b, ToolCall)
        ]
        if calls:
            record["tool_calls"] = calls
        if not record["text"] and not calls:
            return None
    elif isinstance(message, ToolResultMessage):
        record.update(role="tool", tool_call_id=message.tool_call_id, tool=message.tool_name,
                      text=message.text(), is_error=message.is_error)
        if isinstance(message.details, dict):
            record["execution"] = {
                key: message.details[key] for key in ("exit_code", "status", "duration")
                if key in message.details
            }
    else:
        return None
    return record


class BranchSource:
    """Follow descendants of the originating branch, never a newly selected fork."""

    def __init__(self, store):
        self.store = store
        self.anchor = store.leaf
        self.lock = threading.RLock()

    def entries(self) -> list[dict]:
        with self.lock, self.store._lock:
            current = self.store.branch()
            if self.anchor is None or any(e["id"] == self.anchor for e in current):
                self.anchor = self.store.leaf
                return copy.deepcopy(current)
            path, node = [], self.anchor
            while node is not None:
                entry = self.store.by_id.get(node)
                if entry is None:
                    break
                path.append(entry)
                node = entry.get("parent_id")
            return copy.deepcopy(list(reversed(path)))

    def __getattr__(self, name):
        return getattr(self.store, name)

    def append(self, type: str, **data) -> dict:
        """Write review accounting to its task even when another branch is active."""
        try:
            with self.lock, self.store._lock:
                self.entries()
                active = self.store.leaf
                detached = self.anchor != active
                self.store.leaf = self.anchor
                try:
                    if detached:
                        data["active_leaf"] = active
                    return self.store._append_locked(type, **data)
                finally:
                    # Failed writes still allocated a logical entry on this branch.
                    self.anchor = self.store.leaf
                    if detached:
                        self.store.leaf = active
        finally:
            self.store.notify_save_failure()


class ReviewSource:
    """A runtime and the original user-task branch from which it was started."""

    def __init__(self, runtime, parent: ReviewSource | None = None):
        self.runtime = runtime
        self.root = parent.root if parent is not None else BranchSource(runtime.store or runtime.accounting_store)
        self.own = BranchSource(runtime.store) if runtime.store is not None else None
        self.store = self.root.store
        self.workspace = self.store.cwd
        self.history: list[dict] = []
        self.lock = threading.RLock()
        self._next_id = 0
        # Retain a public history for ephemeral subagents across compaction, too.
        self._seen_messages: set[str] = set()
        self._pending_anchors: dict[str, str | None] = {}

    def remember(self, message, entry_id: str | None = None) -> None:
        if self.runtime.store is not None:
            return
        with self.lock:
            token = _message_token(message)
            if token in self._seen_messages:
                return
            self._seen_messages.add(token)
            self._next_id += 1
            identity = f"{self.store.id}:{self.runtime.name}:{entry_id or self._next_id}"
            public = _public_message(message, identity, self.runtime.name, delegated=self.runtime.role != "main")
            if public is not None:
                self.history.append(public)

    def _records(self, entries: list[dict], store, *, delegated: bool, agent: str) -> list[dict]:
        records = []
        for entry in entries:
            identity = f"{store.id}:{entry['id']}"
            if entry["type"] == "message":
                message = message_from_dict(entry["message"])
                record = _public_message(message, identity, agent, delegated=delegated)
                if record:
                    records.append(record)
            elif entry["type"] == "custom" and entry.get("kind") == "permission_decision":
                data = entry.get("data", {})
                records.append({
                    "id": identity, "role": "approval", "agent": data.get("agent", agent),
                    **{key: copy.deepcopy(data[key]) for key in
                       ("tool_call_id", "tool", "mode", "source", "decision", "reason", "arguments_sha256")
                       if key in data},
                })
        return records

    def _with_pending(self, records: list[dict], entries: list[dict], store, *, delegated: bool) -> list[dict]:
        """Keep direct-integration messages before audit events created after first review."""
        positions = {entry["id"]: index for index, entry in enumerate(entries)}
        saved = {entry["id"] for entry in entries if entry["type"] == "message"}
        insertions: dict[int, list[dict]] = {}
        for index, message in enumerate(list(self.runtime.messages)):
            entry_id = self.runtime.ids[index] if index < len(self.runtime.ids) else None
            if entry_id in saved or getattr(message, "kind", None) == "summary":
                continue
            token = _message_token(message)
            with self.lock:
                if token not in self._pending_anchors:
                    self._pending_anchors[token] = entries[-1]["id"] if entries else None
                anchor = self._pending_anchors[token]
            public = _public_message(message, f"{store.id}:pending:{token}", self.runtime.name, delegated=delegated)
            if public is None:
                continue
            anchor_position = positions.get(anchor, -1)
            insertion = sum(positions.get(r["id"].split(":", 1)[-1], -1) <= anchor_position for r in records)
            insertions.setdefault(insertion, []).append(public)
        combined = []
        for index in range(len(records) + 1):
            combined.extend(insertions.get(index, []))
            if index < len(records):
                combined.append(records[index])
        return combined

    def records(self) -> list[dict]:
        root_entries = self.root.entries()
        records = self._records(root_entries, self.store, delegated=False, agent="main")
        rt = self.runtime
        if rt.role == "main":
            records = self._with_pending(records, root_entries, self.store, delegated=False)
        elif self.own is not None:
            own_entries = self.own.entries()
            own_records = self._records(own_entries, self.own.store, delegated=True, agent=rt.name)
            records.extend(self._with_pending(own_records, own_entries, self.own.store, delegated=True))
        else:
            for message in list(rt.messages):
                self.remember(message)
            with self.lock:
                records.extend(copy.deepcopy(self.history))
        return records


@dataclass(frozen=True)
class ReviewInput:
    payload: str
    evidence_ids: frozenset[str]
    audit: dict
    error: str | None
    context_key: str
    store: Any


def source_for(session, ev) -> ReviewSource | None:
    source = getattr(ev, "review_source", None)
    if source is None and ev.agent == "main":
        source = getattr(session.main, "review_source", None)
        # Freeze the fallback onto direct/tool-integration events on first use.
        if source is not None:
            ev.review_source = source
    return source


def review_store(session, ev):
    source = source_for(session, ev)
    return source.root if source is not None else session.store


def review_workspace(session, ev):
    source = source_for(session, ev)
    return source.workspace if source is not None else session.cwd


def review_context_key(session, ev) -> str:
    source = source_for(session, ev)
    if source is None:
        return f"missing:{ev.agent}"
    # Model-call logs and automatic decisions must not invalidate their own review.
    records = [r for r in source.records() if r["role"] == "user" or (
        r["role"] == "approval" and r.get("source") == "human"
    )]
    value = {"task": source.store.id, "workspace": str(source.workspace), "agent": ev.agent,
             "evidence": records, "rules": session.settings.get("permissions", {})}
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _shorten(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    head = length // 2
    return text[:head] + "\n[content omitted]\n" + text[-(length - head):]


def _protected_decision(record: dict) -> bool:
    return record["role"] == "approval" and (
        record.get("source") == "human" or record.get("decision") == "deny"
    )


def build_review_input(session, ev, boundary_reason: str, limit: int = REVIEW_INPUT_LIMIT) -> ReviewInput:
    source = source_for(session, ev)
    store = source.root if source is not None else session.store
    context_key = review_context_key(session, ev)
    action = {"id": f"action:{ev.call_id}", "workspace": str(source.workspace if source else session.cwd),
              "cwd": str(ev.cwd), "agent": ev.agent, "tool": ev.tool_name,
              "arguments": copy.deepcopy(ev.args), "review_reason": boundary_reason}
    if shell := getattr(ev.tool, "shell", None):
        action["shell"] = {"kind": shell.kind, "executable": shell.executable, "version": shell.version}
    records = source.records() if source else []
    original_count = len(records)
    truncated: set[str] = set()
    omitted_users = False
    payload = {"action": action, "transcript": records,
               "context_status": {"omitted_records": 0, "shortened_records": 0, "user_history_complete": source is not None}}

    def encode() -> str:
        available_calls = {call["id"] for record in records for call in record.get("tool_calls", [])}
        for record in records:
            if record["role"] == "approval" and record.get("source") == "human":
                record["scope_available"] = record.get("tool_call_id") in available_calls
        payload["context_status"].update(
            omitted_records=original_count - len(records), shortened_records=len(truncated),
            user_history_complete=source is not None and not omitted_users,
        )
        return _json(payload)

    error = "The originating agent's review context is unavailable." if source is None else None
    # Shorten only public updates/results. Never cut exact operation arguments or
    # a user message into a potentially different instruction.
    for cap in (6000, 1000):
        if cap == 1000 and len(encode()) <= limit:
            break
        for record in records:
            text = record.get("text", "")
            maximum = cap if record["role"] == "tool" else cap // 2
            if record["role"] in {"tool", "assistant", "notification", "context"} and len(text) > maximum:
                record["text"] = _shorten(text, maximum)
                record["truncated"] = True
                truncated.add(record["id"])

    # Old surfaced commentary is less useful than intact calls/results.
    for record in list(records):
        if len(encode()) <= limit:
            break
        if record["role"] in {"notification", "context"} or (
            record["role"] == "assistant" and not record.get("tool_calls")
        ):
            records.remove(record)

    # Remove complete tool batches, including results and approval evidence.
    for record in list(records):
        if len(encode()) <= limit:
            break
        if record not in records:
            continue
        if _protected_decision(record):
            continue
        calls = {c["id"] for c in record.get("tool_calls", [])}
        if record["role"] in {"tool", "approval"}:
            calls.add(record.get("tool_call_id"))
        if calls:
            group = [r for r in records if not _protected_decision(r)
                     and (r is record or r.get("tool_call_id") in calls or
                          any(c["id"] in calls for c in r.get("tool_calls", [])))]
            for member in group:
                records.remove(member)

    # Keep recent user messages whole and ordered; do not select isolated grants.
    last_user = next((r for r in reversed(records) if r["role"] == "user"), None)
    for record in list(records):
        if len(encode()) <= limit:
            break
        if record is last_user:
            continue
        if record["role"] == "user" or _protected_decision(record):
            omitted_users = True
        records.remove(record)
    encoded = encode()
    if len(encoded) > limit:
        error = "The exact action and latest complete user message exceed the automatic review input limit."
    ids = frozenset([action["id"], *(r["id"] for r in records)])
    audit = {
        "context_key": context_key, "context_chars": len(encoded), "context_limit": limit,
        "evidence_ids": sorted(ids), "omitted_records": original_count - len(records),
        "shortened_records": len(truncated), "user_history_complete": source is not None and not omitted_users,
        "user_evidence_ids": [r["id"] for r in records if r["role"] == "user"],
        "human_approval_evidence_ids": [r["id"] for r in records if r["role"] == "approval"
                                        and r.get("source") == "human" and r.get("decision") == "allow"
                                        and r.get("scope_available")],
    }
    return ReviewInput(encoded, ids, audit, error, context_key, store)
