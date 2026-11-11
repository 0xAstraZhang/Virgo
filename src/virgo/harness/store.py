"""Append-only JSONL session tree (≈ Pi's session format).

Line 1 is a header; every other line is an entry ``{type, id, parent_id, timestamp, ...}``.
Entry types: ``message`` (a serialized Message), ``compaction`` (summary + first kept entry),
``model_change`` and ``custom`` (extension state, never sent to the model).
The active branch is the path from the leaf back to the root; nothing is ever rewritten.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

from ..ai.transform import strip_thinking
from ..ai.types import (
    AssistantMessage,
    Message,
    Usage,
    UserMessage,
    message_from_dict,
    message_to_dict,
    text_of,
)

_USAGE_KEYS = ("input", "output", "cache_read", "cache_write")

SUMMARY_PREFIX = (
    "<conversation_summary>\nThe earlier part of this conversation was compacted into the summary below. "
    "It is reference material about prior work, not a new request.\n\n"
)


def summary_message(text: str) -> UserMessage:
    return UserMessage(f"{SUMMARY_PREFIX}{text.strip()}\n</conversation_summary>", kind="summary")


def _without_usage(message: Message) -> Message:
    """Old request usage describes the pre-compaction context, not its replacement."""
    return replace(message, usage=Usage()) if isinstance(message, AssistantMessage) else message


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _created_at(header: dict, fallback: float) -> float:
    try:
        timestamp = datetime.fromisoformat(header["timestamp"]).timestamp()
        return timestamp if math.isfinite(timestamp) else fallback
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return fallback


def _session_info(path: Path) -> dict | None:
    """Read one catalog row, tolerating a corrupt or concurrently removed file."""
    try:
        stat = path.stat()
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not records or not isinstance(records[0], dict) or records[0].get("type") != "session":
            return None
        header = records[0]
        cwd = header.get("cwd", ".")
        if not isinstance(cwd, str):
            return None
        title, count = "", 0
        for entry in records[1:]:
            if not isinstance(entry, dict):
                return None
            if entry.get("type") == "message":
                message = entry["message"]
                if not isinstance(message, dict):
                    return None
                count += 1
                if not title and message.get("role") == "user" and not message.get("kind"):
                    title = " ".join(text_of_dict(message).split())[:80]
        return {
            "path": path,
            "id": header.get("id"),
            "cwd": Path(cwd),
            "mtime": stat.st_mtime,
            "created_at": _created_at(header, getattr(stat, "st_birthtime", stat.st_mtime)),
            "title": title or "(empty)",
            "messages": count,
        }
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _workspace_created_at(directory: Path, items: list[dict] | None = None) -> float:
    try:
        metadata = json.loads((directory / ".workspace.json").read_text(encoding="utf-8"))
        created = float(metadata["created_at"])
        if math.isfinite(created):
            return created
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        pass
    if items is None:
        items = [item for path in directory.glob("*.jsonl") if (item := _session_info(path)) is not None]
    if items:
        return min(item["created_at"] for item in items)
    try:
        stat = directory.stat()
        return getattr(stat, "st_birthtime", stat.st_mtime)
    except OSError:
        return time.time()


def _register_workspace(directory: Path, cwd: Path) -> None:
    """Keep creation order independent of later session writes and removal."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {"version": 1, "cwd": str(cwd), "created_at": _workspace_created_at(directory)}
        with (directory / ".workspace.json").open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False)
            handle.write("\n")
    except OSError:
        # Another process may have registered this directory, or it may be
        # read-only. The catalog can still infer an older workspace's order.
        pass


class SessionStore:
    VERSION = 1

    def __init__(
        self, path: Path | None, cwd: Path, session_id: str | None = None, entries: list | None = None
    ):
        self.path = Path(path).absolute() if path is not None else None  # None -> in memory only
        self.cwd = Path(cwd)
        self.id = session_id or uuid.uuid4().hex[:12]
        self.entries: list[dict] = entries or []
        self.by_id = {e["id"]: e for e in self.entries}
        self.leaf: str | None = self.entries[-1]["id"] if self.entries else None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self._header_written = bool(path and path.exists())
        self._lock = threading.RLock()
        self._usage: tuple[str | None, dict[str, int]] = (None, dict.fromkeys(_USAGE_KEYS, 0))

    # -- construction ---------------------------------------------------------
    @classmethod
    def new(cls, sessions_dir: Path | None, cwd: Path) -> SessionStore:
        sid = uuid.uuid4().hex[:12]
        path = None
        if sessions_dir is not None:
            _register_workspace(Path(sessions_dir), Path(cwd))
            path = Path(sessions_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}_{sid}.jsonl"
        return cls(path, cwd, sid)

    @classmethod
    def load(cls, path: Path) -> SessionStore:
        lines = [
            json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if not lines or lines[0].get("type") != "session":
            raise ValueError(f"{path} is not a Virgo session file")
        header = lines[0]
        store = cls(Path(path), Path(header.get("cwd", ".")), header.get("id"), lines[1:])
        stat = Path(path).stat()
        store.created_at = _created_at(header, getattr(stat, "st_birthtime", stat.st_mtime))
        store.updated_at = stat.st_mtime
        for e in lines[1:]:
            if "active_leaf" in e:  # Late review accounting on an inactive branch.
                store.leaf = e["active_leaf"]
            elif e.get("type") == "leaf":  # explicit leaf moves (branching)
                store.leaf = e["target"]
            else:
                store.leaf = e["id"]
        return store

    @staticmethod
    def list(sessions_dir: Path) -> list[dict]:
        """List one workspace's saved sessions, newest update first."""
        directory = Path(sessions_dir)
        items = [item for path in directory.glob("*.jsonl") if (item := _session_info(path)) is not None]
        created = _workspace_created_at(directory, items)
        for item in items:
            item["workspace_created_at"] = created
        return sorted(items, key=lambda item: (-item["mtime"], str(item["path"])))

    @classmethod
    def list_all(cls, sessions_root: Path) -> list[dict]:
        """List all workspaces' sessions without modifying the catalog on disk."""
        items = [item for directory in Path(sessions_root).glob("*/") for item in cls.list(directory)]
        return sorted(items, key=lambda item: (-item["mtime"], str(item["path"])))

    def describe(self) -> dict:
        """Catalog row for the active store, including an unsaved new session."""
        item = _session_info(self.path) if self.path is not None else None
        if item is None:
            title, count = "", 0
            for entry in self.entries:
                if entry.get("type") == "message":
                    count += 1
                    message = entry["message"]
                    if not title and message.get("role") == "user" and not message.get("kind"):
                        title = " ".join(text_of_dict(message).split())[:80]
            item = {
                "path": self.path,
                "id": self.id,
                "cwd": self.cwd,
                "mtime": self.updated_at,
                "created_at": self.created_at,
                "title": title or "(empty)",
                "messages": count,
            }
        item["workspace_created_at"] = (
            _workspace_created_at(self.path.parent) if self.path is not None else self.created_at
        )
        return item

    def discard_if_empty(self) -> bool:
        """Remove a saved session that has no resumable conversation or state.

        Model choices and leaf moves alone do not change a resumed session.
        Keep every other entry, including unknown extension state, and re-read
        the file so another instance's already-written messages are retained.
        """
        with self._lock:
            if self.path is None or any(entry.get("type") not in {"model_change", "leaf"} for entry in self.entries):
                return False
            try:
                saved = self.load(self.path)
                if saved.id != self.id or any(entry.get("type") not in {"model_change", "leaf"} for entry in saved.entries):
                    return False
                _register_workspace(self.path.parent, self.cwd)
                self.path.unlink()
                self._header_written = False
                return True
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
                return False

    def trash(self) -> None:
        """Move the saved file to the system trash and stop writing to its path."""
        with self._lock:
            if self.path is None:
                return
            try:
                self.path.stat()
            except FileNotFoundError:
                pass  # A new, empty session may not have been written yet.
            else:
                _register_workspace(self.path.parent, self.cwd)
                send2trash(str(self.path))
            self.path = None

    # -- writing --------------------------------------------------------------
    def _write(self, obj: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _register_workspace(self.path.parent, self.cwd)
        with open(self.path, "a", encoding="utf-8") as f:
            if not self._header_written:
                header = {
                    "type": "session",
                    "version": self.VERSION,
                    "id": self.id,
                    "cwd": str(self.cwd),
                    "timestamp": _now(),
                }
                f.write(json.dumps(header, ensure_ascii=False) + "\n")
                self._header_written = True
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def append(self, type: str, **data) -> dict:
        with self._lock:
            entry = {
                "type": type,
                "id": secrets.token_hex(4),
                "parent_id": self.leaf,
                "timestamp": _now(),
                **data,
            }
            while entry["id"] in self.by_id:
                entry["id"] = secrets.token_hex(4)
            self.entries.append(entry)
            self.by_id[entry["id"]] = entry
            self.leaf = entry["id"]
            self.updated_at = time.time()
            self._write(entry)
            return entry

    def append_message(self, msg: Message) -> str:
        return self.append("message", message=message_to_dict(msg))["id"]

    # -- reading --------------------------------------------------------------
    def branch(self) -> list[dict]:
        path, cur = [], self.leaf
        while cur is not None:
            e = self.by_id.get(cur)
            if e is None:
                break
            path.append(e)
            cur = e.get("parent_id")
        path.reverse()
        return path

    def total_usage(self) -> dict[str, int]:
        """Main messages plus auxiliary calls on this branch, including pre-compaction history.

        The status bar asks for this twice a second. Entries are append-only, so
        when the branch has grown from the last counted leaf only the new entries
        are added; after a branch switch the whole branch is counted again.
        """
        counted_leaf, counted = self._usage
        leaf = self.leaf
        new, cur = [], leaf
        while cur is not None and cur != counted_leaf:
            entry = self.by_id.get(cur)
            if entry is None:
                break
            new.append(entry)
            cur = entry.get("parent_id")
        # Without the counted leaf among the ancestors, `new` already holds the whole branch.
        totals = dict(counted) if cur == counted_leaf else dict.fromkeys(_USAGE_KEYS, 0)
        for entry in new:
            message = entry.get("message", {})
            if message.get("role") == "assistant":
                usage = message.get("usage", {})
            elif entry.get("type") == "custom" and entry.get("kind") == "model_call":
                usage = entry.get("data", {}).get("usage", {})
            else:
                continue
            for key in totals:
                totals[key] += usage.get(key, 0)
        self._usage = (leaf, totals)  # one assignment: concurrent readers see a consistent pair
        return dict(totals)

    def build_context(self) -> tuple[list, list]:
        """Messages to send for the active branch, with their entry ids."""
        path = self.branch()
        comp_i = max((i for i, e in enumerate(path) if e["type"] == "compaction"), default=None)
        if comp_i is None:
            return self.build_full_context()
        comp = path[comp_i]
        if "context" in comp:
            messages = [_without_usage(message_from_dict(m)) for m in comp["context"]]
        else:
            messages = [summary_message(comp["summary"])]
        ids = [comp["id"]] * len(messages)
        for i in comp.get("kept_ids", []):
            e = self.by_id.get(i)
            if not e or e["type"] != "message":
                continue
            kept = [message_from_dict(e["message"])]
            if not comp.get("preserve_thinking", False):
                kept = strip_thinking(kept)
            for m in kept:
                messages.append(_without_usage(m))
                ids.append(i)
        for e in path[comp_i + 1 :]:
            if e["type"] == "message":
                messages.append(message_from_dict(e["message"]))
                ids.append(e["id"])
        return messages, ids

    def build_full_context(self) -> tuple[list, list]:
        """Original messages on this branch, for switching away from native compaction."""
        pairs = [
            (message_from_dict(e["message"]), e["id"])
            for e in self.branch()
            if e["type"] == "message"
        ]
        return [m for m, _ in pairs], [i for _, i in pairs]

    def custom(self, kind: str) -> list:
        return [e["data"] for e in self.branch() if e["type"] == "custom" and e.get("kind") == kind]

    def last_compaction(self) -> dict | None:
        for e in reversed(self.branch()):
            if e["type"] == "compaction":
                return e
        return None


def text_of_dict(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if b.get("type") == "text")


__all__ = ["SessionStore", "summary_message", "text_of"]
