"""Bind execution and accounting to the originating conversation and branch."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid


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
        """Write task accounting to its task even when another branch is active."""
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


class ExecutionOrigin:
    """Stable task identity, without a second transcript or inferred authorization."""

    def __init__(self, runtime, parent: ExecutionOrigin | None = None):
        self.runtime = runtime
        self.root = parent.root if parent is not None else BranchSource(runtime.store or runtime.accounting_store)
        self.store = self.root.store
        self.workspace = self.store.cwd
        self.identity = parent.identity if parent is not None else uuid.uuid4().hex


def source_for(session, event) -> ExecutionOrigin | None:
    source = getattr(event, "origin", None)
    if source is None and event.agent == "main":
        source = getattr(session.main, "origin", None)
        # Direct integrations bind once, before a UI selection can change.
        if source is not None:
            event.origin = source
    return source


def origin_store(session, event):
    source = source_for(session, event)
    return source.root if source is not None else session.store


def origin_workspace(session, event):
    source = source_for(session, event)
    return source.workspace if source is not None else session.cwd


def origin_key(session, event) -> str:
    """Invalidate cached authorization only when its explicit policy changes."""
    source = source_for(session, event)
    if source is None:
        return f"missing:{event.agent}"
    provider = session.services["execution"]
    runtime = source.runtime
    snapshot = runtime.permission_snapshot
    value = {
        "origin": source.identity, "workspace": str(source.workspace), "agent": event.agent,
        "provider": provider.name, "context": runtime.execution_context.describe(),
        "approval": snapshot.approval if snapshot is not None else None,
        "revision": provider.revision, "approval_revision": session.approval_revision,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
