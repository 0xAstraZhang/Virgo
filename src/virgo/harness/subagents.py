"""Per-session sub-agent trace storage and reuse registry.

Each sub-agent gets its own ``SessionStore`` under
``<user_dir>/subagents/session-<session_id>/<agent_id>.jsonl`` (or in memory for
ephemeral sessions). Storing sub-agent transcripts separately keeps the main
session JSONL small and failure-isolated while allowing:

- On-demand trace inspection (live or after resuming a saved session)
- Multi-turn reuse of an existing sub-agent within the same parent session
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .store import SessionStore

_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]")
_AGENT_ID_RE = re.compile(r"subagent-([1-9][0-9]*)")


class SubagentError(ValueError):
    """Base error for sub-agent registry operations."""


class SubagentLookupError(SubagentError):
    """Requested sub-agent ID is invalid or does not exist in this session."""


class SubagentBusyError(SubagentError):
    """Requested sub-agent is currently executing another task."""


@dataclass
class SubagentHandle:
    id: str
    store: SessionStore
    description: str = ""
    status: str = "done"  # running | done | error | aborted
    active: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    runs: int = 0
    parent_tool_call_id: str | None = None
    job_id: str | None = None

    def assistant_turns(self) -> int:
        # Compaction only replaces the model-visible context. The full trace
        # keeps earlier turns, including after reopening the parent session.
        messages = (entry["message"] for entry in self.store.entries if entry["type"] == "message")
        return sum(
            message.get("role") == "assistant" and message.get("stop_reason") not in ("turn_limit", "search_paused")
            for message in messages
        )

    def summary(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": "running" if self.active else self.status,
            "active": self.active,
            "runs": self.runs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": self.store.path,
            "parent_tool_call_id": self.parent_tool_call_id,
            "job_id": self.job_id,
        }


def _extract_meta(store: SessionStore) -> tuple[str, str, int]:
    """Read the latest description, status, and run count from a saved trace."""
    description = ""
    status = "done"
    runs = 0
    for entry in store.branch():
        if entry.get("type") == "custom":
            kind = entry.get("kind")
            data = entry.get("data") or {}
            if kind == "subagent_run":
                runs += 1
                if isinstance(data.get("description"), str) and data["description"]:
                    description = data["description"]
            elif kind == "subagent_status":
                if isinstance(data.get("status"), str) and data["status"]:
                    status = data["status"]
    if not description:
        description = store.describe().get("title", "")
        if description == "(empty)":
            description = ""
    return description, status, runs


class SubagentStore:
    """Session-scoped registry of reusable sub-agents and their JSONL traces."""

    def __init__(self, directory: Path, *, persist: bool = True) -> None:
        self.directory = Path(directory)
        self._persist = persist
        self._deleted = False
        self._lock = threading.RLock()
        self._handles: dict[str, SubagentHandle] = {}

    @staticmethod
    def session_directory(user_dir: Path, session_id: str) -> Path:
        safe = _SAFE_SESSION.sub("_", str(session_id or "ephemeral"))[:128] or "ephemeral"
        return Path(user_dir) / "subagents" / f"session-{safe}"

    @classmethod
    def for_session(cls, user_dir: Path, session_id: str, *, persist: bool = True) -> SubagentStore:
        return cls(cls.session_directory(user_dir, session_id), persist=persist)

    @classmethod
    def delete_session(cls, user_dir: Path, session_id: str) -> None:
        shutil.rmtree(cls.session_directory(user_dir, session_id), ignore_errors=True)

    def delete_all(self) -> None:
        with self._lock:
            self._deleted = True
            handles = list(self._handles.values())
            self._handles.clear()
            for handle in handles:
                handle.store.detach()
            shutil.rmtree(self.directory, ignore_errors=True)

    @staticmethod
    def is_valid_id(agent_id: str) -> bool:
        return isinstance(agent_id, str) and bool(_AGENT_ID_RE.fullmatch(agent_id))

    def _next_id_locked(self) -> str:
        highest = 0
        for agent_id in self._handles:
            if match := _AGENT_ID_RE.fullmatch(agent_id):
                highest = max(highest, int(match.group(1)))
        if self._persist and self.directory.is_dir():
            try:
                for path in self.directory.glob("subagent-*.jsonl"):
                    if match := _AGENT_ID_RE.fullmatch(path.stem):
                        highest = max(highest, int(match.group(1)))
            except OSError:
                pass
        return f"subagent-{highest + 1}"

    def _load_handle_locked(self, agent_id: str, *, writable: bool) -> SubagentHandle | None:
        existing = self._handles.get(agent_id)
        if existing is not None:
            return existing
        if not self._persist or not self.is_valid_id(agent_id):
            return None
        path = self.directory / f"{agent_id}.jsonl"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            if writable:
                store, _ = SessionStore.resume(path)
            else:
                store = SessionStore.load(path)
        except (OSError, ValueError):
            return None
        description, status, runs = _extract_meta(store)
        handle = SubagentHandle(
            id=agent_id,
            store=store,
            description=description,
            status=status,
            active=False,
            created_at=store.created_at,
            updated_at=store.updated_at,
            runs=runs,
        )
        self._handles[agent_id] = handle
        return handle

    def acquire(
        self,
        agent_id: str | None,
        *,
        description: str,
        cwd: Path,
        parent_tool_call_id: str | None = None,
        job_id: str | None = None,
    ) -> tuple[SubagentHandle, bool]:
        """Acquire a new or existing sub-agent exclusively for one run."""
        with self._lock:
            if agent_id is not None:
                if not self.is_valid_id(agent_id):
                    raise SubagentLookupError(f"Unknown sub-agent {agent_id!r}.")
                handle = self._load_handle_locked(agent_id, writable=True)
                if handle is None:
                    raise SubagentLookupError(f"Unknown sub-agent {agent_id!r} in this session.")
                if handle.active:
                    raise SubagentBusyError(f"Sub-agent {agent_id!r} is already running.")
                resumed = True
            else:
                new_id = self._next_id_locked()
                path = (self.directory / f"{new_id}.jsonl") if (self._persist and not self._deleted) else None
                store = SessionStore(path, Path(cwd), session_id=new_id)
                handle = SubagentHandle(id=new_id, store=store, description=description)
                self._handles[new_id] = handle
                resumed = False
            handle.active = True
            handle.status = "running"
            if description:
                handle.description = description
            handle.parent_tool_call_id = parent_tool_call_id
            handle.job_id = job_id
            handle.runs += 1
            handle.updated_at = time.time()
            return handle, resumed

    def release(self, handle: SubagentHandle, *, status: str) -> None:
        with self._lock:
            handle.active = False
            handle.status = status
            handle.updated_at = time.time()
            try:
                if not self._deleted and handle.store.path is not None and handle.store.save_error is None:
                    handle.store.append("custom", kind="subagent_status", data={"status": status})
            except OSError:
                pass

    def record_run(
        self,
        handle: SubagentHandle,
        *,
        description: str,
        parent_tool_call_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        with self._lock:
            if self._deleted:
                return
            handle.store.append(
                "custom",
                kind="subagent_run",
                data={
                    "description": description,
                    "parent_tool_call_id": parent_tool_call_id,
                    "job_id": job_id,
                },
            )

    def get(self, agent_id: str) -> SubagentHandle | None:
        with self._lock:
            return self._load_handle_locked(agent_id, writable=False)

    def entries(self, agent_id: str) -> list[dict]:
        with self._lock:
            handle = self._load_handle_locked(agent_id, writable=False)
            if handle is None:
                return []
            return handle.store.branch_snapshot()

    def list(self) -> list[dict]:
        with self._lock:
            if self._persist and self.directory.is_dir():
                try:
                    for path in self.directory.glob("subagent-*.jsonl"):
                        if self.is_valid_id(path.stem):
                            self._load_handle_locked(path.stem, writable=False)
                except OSError:
                    pass
            items = [handle.summary() for handle in self._handles.values()]
        items.sort(key=lambda item: (-item["updated_at"], item["id"]))
        return items
