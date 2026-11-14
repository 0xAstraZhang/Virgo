"""Small shared request budget and downward-only runtime cancellation."""
from __future__ import annotations

import threading
import time


class RequestBudget:
    """One active main conversation and all of its children share this counter."""

    def __init__(self, limit: int | None):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def consume(self):
        with self._lock:
            if self.limit is not None and self.used >= self.limit:
                raise RuntimeError(f"Shared model request budget exhausted ({self.limit} calls)")
            self.used += 1


class ChildCancellation:
    """Observe the parent without allowing a child abort to cancel its parent."""

    def __init__(self, parent, local=None):
        self.parent = parent
        self.local = local if local is not None and local is not parent else threading.Event()

    def is_set(self):
        if self.parent.is_set():
            self.local.set()
        return self.local.is_set()

    def set(self):
        self.local.set()

    def clear(self):
        self.local.clear()

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = deadline - time.monotonic() if deadline is not None else 0.05
            if remaining <= 0:
                return self.is_set()
            self.local.wait(min(0.05, remaining))
        return True
