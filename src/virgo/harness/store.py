"""Append-only JSONL session tree (≈ Pi's session format).

Line 1 is a header; every other line is an entry ``{type, id, parent_id, timestamp, ...}``.
Entry types: ``message`` (a serialized Message), ``compaction`` (summary + first kept entry),
``model_change`` and ``custom`` (extension state, never sent to the model).
The active branch is the path from the leaf back to the root; nothing is ever rewritten.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from pathlib import Path

from ..ai.transform import strip_thinking
from ..ai.types import Message, UserMessage, message_from_dict, message_to_dict, text_of

SUMMARY_PREFIX = (
    "<conversation_summary>\nThe earlier part of this conversation was compacted into the summary below. "
    "It is reference material about prior work, not a new request.\n\n"
)


def summary_message(text: str) -> UserMessage:
    return UserMessage(f"{SUMMARY_PREFIX}{text.strip()}\n</conversation_summary>", kind="summary")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class SessionStore:
    VERSION = 1

    def __init__(
        self, path: Path | None, cwd: Path, session_id: str | None = None, entries: list | None = None
    ):
        self.path = path  # None -> in memory only
        self.cwd = Path(cwd)
        self.id = session_id or uuid.uuid4().hex[:12]
        self.entries: list[dict] = entries or []
        self.by_id = {e["id"]: e for e in self.entries}
        self.leaf: str | None = self.entries[-1]["id"] if self.entries else None
        self._header_written = bool(path and path.exists())
        self._lock = threading.Lock()

    # -- construction ---------------------------------------------------------
    @classmethod
    def new(cls, sessions_dir: Path | None, cwd: Path) -> SessionStore:
        sid = uuid.uuid4().hex[:12]
        path = None
        if sessions_dir is not None:
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
        for e in lines[1:]:
            if e.get("type") == "leaf":  # explicit leaf moves (branching)
                store.leaf = e["target"]
        return store

    @staticmethod
    def list(sessions_dir: Path) -> list[dict]:
        out = []
        for p in sorted(Path(sessions_dir).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            title, count = "", 0
            try:
                for line in p.read_text(encoding="utf-8").splitlines()[1:]:
                    e = json.loads(line)
                    if e.get("type") == "message":
                        count += 1
                        m = e["message"]
                        if not title and m.get("role") == "user" and not m.get("kind"):
                            title = text_of_dict(m)[:80]
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {"path": p, "mtime": p.stat().st_mtime, "title": title or "(empty)", "messages": count}
            )
        return out

    # -- writing --------------------------------------------------------------
    def _write(self, obj: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def build_context(self) -> tuple[list, list]:
        """Messages to send for the active branch, with their entry ids."""
        path = self.branch()
        comp_i = max((i for i, e in enumerate(path) if e["type"] == "compaction"), default=None)
        if comp_i is None:
            pairs = [(message_from_dict(e["message"]), e["id"]) for e in path if e["type"] == "message"]
            return [m for m, _ in pairs], [i for _, i in pairs]
        comp = path[comp_i]
        messages, ids = [summary_message(comp["summary"])], [comp["id"]]
        for i in comp.get(
            "kept_ids", []
        ):  # retained turns lose their thinking blocks (they predate the summary)
            e = self.by_id.get(i)
            if not e or e["type"] != "message":
                continue
            for m in strip_thinking([message_from_dict(e["message"])]):
                messages.append(m)
                ids.append(i)
        for e in path[comp_i + 1 :]:
            if e["type"] == "message":
                messages.append(message_from_dict(e["message"]))
                ids.append(e["id"])
        return messages, ids

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
