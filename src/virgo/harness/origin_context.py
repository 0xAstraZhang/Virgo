"""Bind execution and accounting to the originating conversation and branch."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid

from .store import SessionSaveError


class BranchSource:
    """Follow descendants of the originating branch, never a newly selected fork."""

    def __init__(self, store):
        self.store = store
        self.anchor = store.leaf
        self.preset_revision = 0
        self.lock = threading.RLock()

    def entries(self) -> list[dict]:
        with self.lock:
            self.anchor, items = self.store.branch_entries(self.anchor)
            return items

    def __getattr__(self, name):
        return getattr(self.store, name)

    def append(self, type: str, **data) -> dict:
        """Write task accounting to its task even when another branch is active."""
        with self.lock:
            try:
                self.anchor, entry = self.store.append_on_branch(self.anchor, type, **data)
                return entry
            except SessionSaveError as error:
                # Failed writes still allocated a logical entry on this branch.
                self.anchor = error.entry["id"]
                raise


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
        "provider": runtime.execution_context.provider, "context": runtime.execution_context.describe(),
        "approval": snapshot.approval if snapshot is not None else None,
        "revision": provider.revision, "preset_revision": snapshot.revision if snapshot is not None else 0,
        "approval_revision": session.approval_revision,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
