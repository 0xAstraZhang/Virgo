"""Append-only JSONL session tree.

The first nonblank line is a header; subsequent records are entries ``{type, id, parent_id, timestamp, ...}``.
Entry types: ``message`` (a serialized Message), ``compaction`` (summary + first kept entry),
``model_change`` and ``custom`` (extension state, never sent to the model).
The active branch is the path from the leaf back to the root. Writable resume may
discard an undecodable, unterminated final record.
Tool-result entries keep boundary outcomes on disk. Message entries also retain a
grant summary for the native process, or for file tools when no native policy applied.
"""

from __future__ import annotations

import copy
import json
import math
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from send2trash import send2trash

from ..ai.types import (
    Message,
    message_from_dict,
    message_to_dict,
)
from ..tools.fs import file_lock
from .compaction_records import (
    SUMMARY_PREFIX,
    compacted_message,
    replay_compaction_context,
    summary_message,
    validate_compaction_record,
)
from .compaction_records import (
    stored_details as _stored_details,
)

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        # File locks use the two DWORD offsets in OVERLAPPED's union.
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    )
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
else:
    import fcntl

_USAGE_KEYS = ("input", "output", "cache_read", "cache_write")


def _entry_for_disk(entry: dict) -> dict:
    """Compact tool-result policy metadata without changing the live session tree."""
    match entry:
        case {"type": "message", "message": {"role": "toolResult"} as message}:
            transcript = False
        case {"type": "custom", "kind": "transcript_tool_result", "data": {"message": dict() as message}}:
            transcript = True
        case _:
            return entry

    details = message.get("details")
    stored = _stored_details(details, transcript=transcript)
    if stored is details:
        return entry
    message = {**message, "details": stored}
    if transcript:
        return {**entry, "data": {**entry["data"], "message": message}}
    return {**entry, "message": message}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _created_at(header: dict, fallback: float) -> float:
    try:
        timestamp = datetime.fromisoformat(header["timestamp"]).timestamp()
        return timestamp if math.isfinite(timestamp) else fallback
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return fallback


def _title_and_count(entries: list) -> tuple[str, int]:
    """The first genuine user request and the number of validated message entries."""
    title, count = "", 0
    for entry in entries:
        if entry.get("type") == "message":
            message = entry["message"]
            count += 1
            if not title and message.get("role") == "user" and not message.get("kind"):
                title = " ".join(text_of_dict(message).split())[:80]
    return title, count


@contextmanager
def _locked_file(path: Path, mode: str):
    """Hold a shared read lock or an exclusive write lock through the final flush."""
    with file_lock(path.resolve()), open(path, mode) as handle:
        if os.name != "nt":
            fcntl.flock(handle, fcntl.LOCK_SH if mode == "rb" else fcntl.LOCK_EX)
            yield handle
            return  # Close flushes before releasing flock.

        native_handle = msvcrt.get_osfhandle(handle.fileno())
        offset = _Overlapped()  # Zero offset and event for a synchronous file handle.
        flags = 0 if mode == "rb" else _LOCKFILE_EXCLUSIVE_LOCK
        if not _kernel32.LockFileEx(native_handle, flags, 0, 1, 0, ctypes.byref(offset)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield handle
        finally:
            handle.flush()
            if not _kernel32.UnlockFileEx(native_handle, 0, 1, 0, ctypes.byref(offset)):
                raise ctypes.WinError(ctypes.get_last_error())


@dataclass(frozen=True)
class _IncompleteTail:
    line: int
    offset: int
    size: int


def _validate_message(message: dict) -> None:
    if not isinstance(message, dict):
        raise ValueError("expected a message object")
    role = message.get("role")
    if role not in ("user", "assistant", "toolResult"):
        raise ValueError("missing or unknown message role")
    content = message.get("content", [] if role == "assistant" else None)
    if not isinstance(content, list) and not (role == "user" and isinstance(content, str)):
        raise ValueError("invalid message content")
    if role == "assistant" and "usage" in message:
        usage = message["usage"]
        if not isinstance(usage, dict) or any(isinstance(n, bool) or not isinstance(n, int) for n in usage.values()):
            raise ValueError("invalid message usage")
    # Use the same block constructors and legacy field conversions as runtime replay.
    try:
        message_from_dict(message).text()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid message: {error}") from error


def _validate_record(record: dict, *, header: bool, ids: set[str], absent_parents: set[str]) -> None:
    if not isinstance(record, dict):
        raise ValueError("expected a session header object" if header else "expected an entry object")
    kind = record.get("type")
    if header and kind != "session":
        raise ValueError("expected a session header")
    if not isinstance(record.get("id"), str) or not record["id"].strip():
        raise ValueError("missing or invalid record id")
    if header:
        if not isinstance(record.get("cwd"), str) or not record["cwd"].strip():
            raise ValueError("missing or invalid session header cwd")
        return
    if not isinstance(kind, str) or not kind.strip() or kind == "session":
        raise ValueError("invalid entry type")
    entry_id = record["id"]
    if entry_id in ids:
        raise ValueError("duplicate entry id")
    parent = record.get("parent_id")
    if parent is not None:
        if not isinstance(parent, str) or not parent.strip():
            raise ValueError("invalid parent_id")
        if parent not in ids:
            # Empty-session cleanup can retire a model_change parent. branch()
            # treats a missing parent as a root. It must never become a forward edge.
            absent_parents.add(parent)
    if entry_id in absent_parents:
        raise ValueError("forward or cyclic parent_id")
    if kind == "leaf" and "target" not in record:
        raise ValueError("missing leaf target")
    for key in ("active_leaf", "target") if kind == "leaf" else ("active_leaf",):
        value = record.get(key)
        if value is not None and (not isinstance(value, str) or value not in ids):
            raise ValueError(f"invalid {key}: expected an earlier entry id or null")
    if kind == "message":
        _validate_message(record.get("message"))
    elif kind == "compaction":
        validate_compaction_record(record)
    elif kind == "custom" and "data" not in record:
        raise ValueError("missing custom entry data")
    ids.add(record["id"])


def _invalid_session(path: Path, line: int, reason: str) -> ValueError:
    return ValueError(f"{path}, line {line}: {reason}. Choose another saved session explicitly with -r <path>.")


def _read_records(handle: BinaryIO, path: Path) -> tuple[list[dict], _IncompleteTail | None]:
    records = []
    ids: set[str] = set()
    absent_parents: set[str] = set()
    offset = 0
    for number, raw in enumerate(handle, 1):
        if raw.strip():
            try:
                record = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                split_utf8 = isinstance(error, UnicodeDecodeError) and error.reason == "unexpected end of data"
                if records and not raw.endswith(b"\n") and (isinstance(error, json.JSONDecodeError) or split_utf8):
                    return records, _IncompleteTail(number, offset, len(raw))
                reason = "invalid UTF-8" if isinstance(error, UnicodeDecodeError) else f"invalid JSON ({error.msg})"
                if not records:
                    reason = f"invalid session header: {reason}"
                raise _invalid_session(path, number, reason) from error
            except (RecursionError, ValueError) as error:
                # Decoder limits (nesting or integer size) can reject complete values.
                raise _invalid_session(path, number, f"unreadable JSON: {error}") from error
            try:
                _validate_record(record, header=not records, ids=ids, absent_parents=absent_parents)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise _invalid_session(path, number, str(error)) from error
            records.append(record)
        offset += len(raw)
    if not records:
        raise _invalid_session(path, 1, "missing session header")
    return records, None


def _session_info(path: Path) -> dict | None:
    """Read one catalog row, tolerating a corrupt or concurrently removed file."""
    try:
        store = SessionStore.load(path)
        title, count = _title_and_count(store.entries)
        return {
            "path": path, "id": store.id, "cwd": store.cwd, "mtime": store.updated_at,
            "created_at": store.created_at, "title": title or "(empty)", "messages": count,
        }
    except (OSError, ValueError):
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
        if (directory / ".workspace.json").exists():
            return  # Already registered; this runs on every session write.
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {"version": 1, "cwd": str(cwd), "created_at": _workspace_created_at(directory)}
        with (directory / ".workspace.json").open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False)
            handle.write("\n")
    except OSError:
        # Another process may have registered this directory, or it may be
        # read-only. The catalog can still infer an older workspace's order.
        pass


class SessionSaveError(OSError):
    """The logical entry exists, but its disk write was not confirmed."""

    def __init__(self, store, entry, cause):
        self.store, self.entry, self.cause = store, entry, cause
        super().__init__(f"Session {store.id}: unsaved history at {store.path}: {cause}. "
                         "Work is paused; unsaved content exists only in this process.")


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
        self._tail: _IncompleteTail | None = None
        self._lock = threading.RLock()
        self.save_error: SessionSaveError | None = None
        self.last_saved_id = self.leaf
        self.failure_signals: list[threading.Event] = []
        self.failure_listeners: list = []
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
        """Inspect a saved tree without modifying the session file."""
        path = Path(path)
        with _locked_file(path, "rb") as handle:
            return cls._read(path, handle)

    @classmethod
    def _read(cls, path: Path, handle: BinaryIO) -> SessionStore:
        lines, tail = _read_records(handle, path)
        header = lines[0]
        store = cls(path, Path(header["cwd"]), header["id"], lines[1:])
        store._tail = tail
        stat = os.fstat(handle.fileno())
        store.created_at = _created_at(header, getattr(stat, "st_birthtime", stat.st_mtime))
        store.updated_at = stat.st_mtime
        if store.entries:
            last = store.entries[-1]
            if "active_leaf" in last:  # Late review accounting on an inactive branch.
                store.leaf = last["active_leaf"]
            elif last["type"] == "leaf":  # Explicit leaf move.
                store.leaf = last["target"]
        return store

    @classmethod
    def resume(cls, path: Path) -> tuple[SessionStore, _IncompleteTail | None]:
        """Recheck a torn tail under writer exclusion before removing it."""
        path = Path(path)
        store = cls.load(path)
        tail = store._tail
        if tail is not None:
            line = tail.line
            try:
                with _locked_file(path, "r+b") as handle:
                    store = cls._read(path, handle)
                    tail = store._tail
                    if tail is not None:
                        line = tail.line
                        handle.truncate(tail.offset)
                        store.updated_at = os.fstat(handle.fileno()).st_mtime
                        store._tail = None
            except OSError as error:
                raise _invalid_session(path, line, f"could not repair the trailing record: {error}") from error
        return store, tail

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
        # A readable prefix can still omit unsaved entries. Its catalog must
        # describe the preserved logical history once persistence has failed.
        item = _session_info(self.path) if self.path is not None and self.save_error is None else None
        if item is None:
            title, count = _title_and_count(self.entries)
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
            if self.save_error is not None or self.path is None or any(entry.get("type") not in {"model_change", "leaf"} for entry in self.entries):
                return False
            try:
                saved = self.load(self.path)
                if saved._tail is not None or saved.id != self.id or any(entry.get("type") not in {"model_change", "leaf"} for entry in saved.entries):
                    return False
                _register_workspace(self.path.parent, self.cwd)
                self.path.unlink()
                return True
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
                return False

    def register_failure_handler(
        self,
        *,
        signal: threading.Event | None = None,
        listener: Callable[[SessionSaveError], None] | None = None,
    ) -> Callable[[], None]:
        """Register a cancellation signal and/or error listener without exposing store locks."""
        with self._lock:
            if signal is not None and signal not in self.failure_signals:
                self.failure_signals.append(signal)
            if listener is not None and listener not in self.failure_listeners:
                self.failure_listeners.append(listener)
            if self.save_error is not None and signal is not None:
                signal.set()

        def unregister() -> None:
            with self._lock:
                if signal is not None and signal in self.failure_signals:
                    self.failure_signals.remove(signal)

        return unregister

    def detach(self) -> None:
        """Stop persisting further entries to disk without touching any existing file."""
        with self._lock:
            self.path = None

    def branch_snapshot(self) -> list[dict]:
        """Return a thread-safe deep copy of the active branch."""
        with self._lock:
            return copy.deepcopy(self.branch())

    def branch_entries(self, anchor: str | None) -> tuple[str | None, list[dict]]:
        """Resolve an anchored branch snapshot under the store lock."""
        with self._lock:
            current = self.branch()
            if anchor is None or any(e["id"] == anchor for e in current):
                return self.leaf, copy.deepcopy(current)
            path, node = [], anchor
            while node is not None:
                entry = self.by_id.get(node)
                if entry is None:
                    break
                path.append(entry)
                node = entry.get("parent_id")
            return anchor, copy.deepcopy(list(reversed(path)))

    def append_on_branch(self, anchor: str | None, type: str, **data) -> tuple[str | None, dict]:
        """Append to an anchored branch even when another branch is active."""
        try:
            with self._lock:
                resolved_anchor, _ = self.branch_entries(anchor)
                active = self.leaf
                detached = resolved_anchor != active
                self.leaf = resolved_anchor
                try:
                    if detached:
                        data["active_leaf"] = active
                    entry = self._append_locked(type, **data)
                finally:
                    new_anchor = self.leaf
                    if detached:
                        self.leaf = active
                return new_anchor, entry
        finally:
            self.notify_save_failure()

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

    @classmethod
    def trash_all(cls, stores: Sequence[SessionStore]) -> None:
        """Atomically lock all live store handles for a path, trash once, and detach aliases."""
        if not stores:
            return
        with ExitStack() as locks:
            for store in stores:
                locks.enter_context(store._lock)
            stores[0].trash()
            for store in stores[1:]:
                store.path = None

    # -- writing --------------------------------------------------------------
    def _write(self, obj: dict) -> None:
        if self.path is None:
            return
        if self._tail is not None:
            raise ValueError(f"Resume {self.path} before appending to its incomplete trailing record")
        line = json.dumps(_entry_for_disk(obj), ensure_ascii=False, default=str).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _register_workspace(self.path.parent, self.cwd)
        with _locked_file(self.path, "a+b") as handle:
            if handle.seek(0, os.SEEK_END):
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    line = b"\n" + line
            else:
                header = {"type": "session", "version": self.VERSION, "id": self.id,
                          "cwd": str(self.cwd), "timestamp": _now()}
                line = json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n" + line
            handle.write(line)

    def notify_save_failure(self) -> None:
        if self.save_error is not None:
            for callback in tuple(self.failure_listeners):
                try:
                    callback(self.save_error)
                except Exception:
                    pass  # Notification failure cannot erase the persistence fault.

    def append(self, type: str, **data) -> dict:
        try:
            with self._lock:
                return self._append_locked(type, **data)
        except SessionSaveError:
            self.notify_save_failure()
            raise

    def _append_locked(self, type: str, **data) -> dict:
        """Caller holds _lock and must notify after releasing it on failure."""
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
            if self.save_error is not None:
                raise SessionSaveError(self, entry, self.save_error.cause)
            try:
                self._write(entry)
            except Exception as cause:
                self.save_error = SessionSaveError(self, entry, cause)
                # Cancellation only: never invoke UI, extensions or Session locks
                # while a writer (including BranchSource) holds the store lock.
                for signal in self.failure_signals:
                    signal.set()
                raise self.save_error from cause
            self.last_saved_id = entry["id"]
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
        return replay_compaction_context(self.branch(), self.by_id, created_at=_created_at)

    def build_full_context(self) -> tuple[list, list]:
        """Original uncompacted messages on this branch."""
        pairs = [
            (message_from_dict(e["message"]), e["id"])
            for e in self.branch()
            if e["type"] == "message"
        ]
        return [m for m, _ in pairs], [i for _, i in pairs]

    def custom(self, kind: str) -> list:
        return [e["data"] for e in self.branch() if e["type"] == "custom" and e.get("kind") == kind]

    def last_compaction(self, *, mode: str | None = "summary") -> dict | None:
        for e in reversed(self.branch()):
            if e["type"] == "compaction" and (mode is None or e.get("mode", "summary") == mode):
                return e
        return None


def text_of_dict(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if b.get("type") == "text")


__all__ = ["SUMMARY_PREFIX", "SessionStore", "compacted_message", "summary_message"]
