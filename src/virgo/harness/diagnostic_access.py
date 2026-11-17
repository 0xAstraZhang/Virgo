"""Explicit, runtime-local access to bounded diagnostic evidence.

An error ID is a reference, not authority. Only ``remember`` grants access; neither
the active UI session nor a shared accounting store expands this collection. The
owner restores grants from its own canonical history. No model-supplied paths or
diagnostic log files are opened here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

from ..diagnostics import sanitize
from .diagnostic_store import connect

SECTIONS = ("summary", "causes", "trace", "stderr", "attempts", "related")
MAX_RESPONSE_BYTES = 8192
MAX_RECORD_BYTES = 64 * 1024
_ERROR_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CURSOR = re.compile(r"([a-f0-9]{24}):([0-9]{1,19}):([0-9]{1,8})\Z")
_END = 2**63 - 1
_CALL_KEYS = ("model_call_id", "runtime_id", "session_id", "run_id", "agent", "purpose")
_RECORD_FIELDS = (
    "schema_version", "error_id", "timestamp", "code", "phase", "kind", "message", "exception_type",
    "traceback", "chain_truncated", "record_truncated", "logging_error", "record_warning",
)
_CONTEXT_FIELDS = (
    "runtime_id", "session_id", "accounting_session_id", "run_id", "agent", "role", "tool_call_id",
    "tool_name", "model_call_id", "model", "api", "purpose", "attempt", "max_retries", "duration",
    "server", "generation", "candidate_generation", "dispatch", "method", "mcp_tool", "request_id",
    "rpc_code", "exit_code", "status", "timeout", "errno", "outcome", "side_effects", "protocol_error",
    "stderr_tail", "stderr_truncated",
)
_HTTP_FIELDS = ("status_code", "request_id", "retry_after", "endpoint")
_EXECUTION_FIELDS = ("started", "outcome", "side_effects", "result_hook")
_TRANSPORT_FIELDS = (
    "protocol", "server", "generation", "candidate_generation", "dispatch", "outcome", "side_effects",
    "method", "mcp_tool", "request_id",
)
_SUMMARY_FIELDS = tuple(key for key in _RECORD_FIELDS if key != "traceback")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _fields(value, names) -> dict:
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in names
            if name in value and (value[name] is None or type(value[name]) in (str, bool, int, float))}


def _clip_strings(value, limit):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "\n[truncated]"
    if isinstance(value, list):
        return [_clip_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _clip_strings(item, limit) for key, item in value.items()}
    return value


def _record_view(record) -> dict | None:
    clean = sanitize(record)
    if not isinstance(clean, dict):
        return None
    error_id = clean.get("error_id")
    if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
        return None
    result = _fields(clean, _RECORD_FIELDS)
    context = clean.get("context")
    result["context"] = _fields(context, _CONTEXT_FIELDS)
    if isinstance(context, dict):
        for key, names in (("http", _HTTP_FIELDS), ("execution", _EXECUTION_FIELDS),
                           ("transport", _TRANSPORT_FIELDS)):
            if isinstance(context.get(key), dict):
                result["context"][key] = _fields(context[key], names)
    chain = clean.get("cause_chain")
    if isinstance(chain, list):
        result["cause_chain"] = []
        for cause in chain[:8]:
            if not isinstance(cause, dict):
                continue
            item = _fields(cause, ("exception_type", "message", "relation", "omitted_frames"))
            frames = cause.get("frames")
            if isinstance(frames, list):
                item["frames"] = [_fields(frame, ("file", "line", "function")) for frame in frames[-16:]
                                  if isinstance(frame, dict)]
                if len(frames) > 16:
                    previous = item.get("omitted_frames")
                    item["omitted_frames"] = (previous if type(previous) is int else 0) + len(frames) - 16
                    result["record_truncated"] = True
            children = cause.get("exceptions")
            if isinstance(children, list):
                item["exceptions"] = [_fields(child, ("exception_type", "message")) for child in children[:16]
                                      if isinstance(child, dict)]
                if len(children) > 16:
                    result["record_truncated"] = True
            result["cause_chain"].append(item)
        if len(chain) > 8:
            result["chain_truncated"] = True
    related = clean.get("related_error_ids", clean.get("related_errors"))
    if isinstance(related, list):
        result["related_error_ids"] = []
        for item in related[:64]:
            reference = item.get("error_id") if isinstance(item, dict) else item
            if isinstance(reference, str) and _ERROR_ID.fullmatch(reference):
                if reference not in result["related_error_ids"]:
                    result["related_error_ids"].append(reference)
    for limit in (16384, 8192, 4096, 2048, 1024, 512, 256, 128, 64):
        if len(_json(result).encode("utf-8")) <= MAX_RECORD_BYTES:
            return result
        result = _clip_strings(result, limit)
        result["error_id"] = error_id
        result["record_truncated"] = True
    # Fixed schemas and bounded lists fit the limit above. Retain a safe reference
    # if a future schema expansion accidentally exceeds that invariant.
    return {"error_id": error_id, "message": "Diagnostic evidence exceeded its retention budget.",
            "record_truncated": True, "context": {}}


class DiagnosticAccess:
    """Disk-backed private snapshots. Only remember grants access; no global fallback."""

    def __init__(self, *, directory: Path | None = None, page_bytes: int = MAX_RESPONSE_BYTES):
        if type(page_bytes) is not int or not 512 <= page_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError(f"page_bytes must be between 512 and {MAX_RESPONSE_BYTES}")
        self.page_bytes = page_bytes
        self.directory = directory or Path(os.environ.get("VIRGO_HOME", Path.home() / ".virgo")) / "logs"
        self._lock = threading.RLock()
        self._connection = None
        self._temporary = None
        self._closed = False
        self._version = 0
        self._pending_sequence = 0
        self._cursor_key = uuid.uuid4().bytes

    def _db(self):
        if self._closed:
            raise OSError("Runtime diagnostic evidence has been closed")
        if self._connection is None:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(prefix="runtime-", dir=self.directory)
            db = None
            try:
                db = connect(Path(temporary.name) / "evidence.sqlite3", durable=False)
                db.executescript("""
                    CREATE TABLE evidence (
                        seq INTEGER PRIMARY KEY, error_id TEXT NOT NULL UNIQUE,
                        call_key TEXT NOT NULL, payload TEXT NOT NULL
                    );
                    CREATE INDEX evidence_call ON evidence(call_key, seq);
                    CREATE TABLE delivery (
                        error_id TEXT PRIMARY KEY, recorded INTEGER NOT NULL DEFAULT 0,
                        presented INTEGER NOT NULL DEFAULT 0, pending INTEGER
                    );
                    CREATE INDEX delivery_pending ON delivery(pending) WHERE pending IS NOT NULL;
                """)
            except BaseException:
                if db is not None:
                    db.close()
                temporary.cleanup()
                raise
            self._connection, self._temporary = db, temporary
        return self._connection

    def close(self):
        with self._lock:
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def __del__(self):
        if getattr(self, "_connection", None) is not None:
            self._connection.close()

    def remember(self, record: dict) -> dict | None:
        """Persist a detached, filtered snapshot in this runtime's private database."""
        view = _record_view(record)
        if view is None:
            return None
        payload = _json(view)
        call_key = _json([view["context"].get(key) for key in _CALL_KEYS])
        with self._lock:
            changed = self._db().execute("""
                INSERT INTO evidence(error_id, call_key, payload) VALUES (?, ?, ?)
                ON CONFLICT(error_id) DO UPDATE SET call_key=excluded.call_key, payload=excluded.payload
                WHERE evidence.payload != excluded.payload
            """, (view["error_id"], call_key, payload)).rowcount
            self._version += bool(changed)
        return view

    def get(self, error_id: str) -> dict | None:
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return None
        with self._lock:
            if self._connection is None and not self._closed:
                return None
            row = self._db().execute("SELECT payload FROM evidence WHERE error_id=?", (error_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            if self._connection is None and not self._closed:
                return []
            rows = self._db().execute("SELECT payload FROM evidence ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(row[0]) for row in reversed(rows)]

    def mark(self, error_id: str, action: str) -> bool:
        """Delivery bookkeeping is disk-backed too; it cannot create evidence grants."""
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return False
        with self._lock:
            db = self._db()
            db.execute("INSERT OR IGNORE INTO delivery(error_id) VALUES (?)", (error_id,))
            if action == "record":
                return bool(db.execute("UPDATE delivery SET recorded=1 WHERE error_id=? AND recorded=0",
                                       (error_id,)).rowcount)
            if action == "presented":
                return bool(db.execute("UPDATE delivery SET presented=1, pending=NULL WHERE error_id=? AND presented=0",
                                       (error_id,)).rowcount)
            if action == "pending":
                self._pending_sequence += 1
                return bool(db.execute("""UPDATE delivery SET pending=?
                    WHERE error_id=? AND presented=0 AND pending IS NULL""",
                                       (self._pending_sequence, error_id)).rowcount)
            raise ValueError("Unknown diagnostic delivery action")

    def pending(self, limit: int = 3) -> list[str]:
        with self._lock:
            if self._connection is None and not self._closed:
                return []
            return [row[0] for row in self._db().execute(
                "SELECT error_id FROM delivery WHERE pending IS NOT NULL ORDER BY pending LIMIT ?", (limit,))]

    @staticmethod
    def _summary(record: dict) -> dict:
        result = {key: record[key] for key in _SUMMARY_FIELDS if key in record}
        result["context"] = {key: value for key, value in record.get("context", {}).items() if key != "stderr_tail"}
        result["cause_count"] = len(record.get("cause_chain", []))
        return result

    def _segments(self, record, section, position):
        """Resume at a row key, never serialize or scan all previous result rows."""
        if section in ("summary", "causes", "trace", "stderr"):
            value = {"summary": lambda: _json(self._summary(record)),
                     "causes": lambda: _json(record["cause_chain"]) if record.get("cause_chain") else None,
                     "trace": lambda: record.get("traceback"),
                     "stderr": lambda: record.get("context", {}).get("stderr_tail")}[section]()
            if isinstance(value, str) and value and position == 0:
                yield 0, value
            return
        if section == "attempts":
            context = record.get("context", {})
            if not context.get("model_call_id"):
                return
            key = _json([context.get(name) for name in _CALL_KEYS])
            db = self._db()
            first = db.execute("SELECT seq FROM evidence WHERE call_key=? ORDER BY seq LIMIT 1", (key,)).fetchone()[0]
            if position == 0:
                yield 0, '{"scope":"retained_authorized_failures","records":['
            for seq, payload in db.execute(
                "SELECT seq, payload FROM evidence WHERE call_key=? AND seq>=? ORDER BY seq", (key, max(1, position))):
                yield seq, ("" if seq == first else ",") + _json(self._summary(json.loads(payload)))
        else:
            if position == 0:
                yield 0, '{"scope":"explicitly_authorized_references","records":['
            seen = False
            for index, reference in enumerate(record.get("related_error_ids", []), 1):
                related = self.get(reference)
                if related is not None:
                    if index >= position:
                        yield index, ("," if seen else "") + _json(self._summary(related))
                    seen = True
        yield _END, "]}"

    def _token(self, error_id, section, position, offset):
        data = f"{self._version}\0{error_id}\0{section}\0{position}\0{offset}"
        signature = hmac.new(self._cursor_key, data.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{signature}:{position}:{offset}"

    def read(self, error_id: str, section: str = "summary", cursor: str | None = None) -> dict:
        """Return an 8 KiB envelope; concatenate text pages to reconstruct a section."""
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return {"status": "not_found", "message": "No diagnostic with that ID is available to this runtime."}
        if section not in SECTIONS:
            return {"status": "invalid_section", "message": "Choose summary, causes, trace, stderr, attempts or related."}
        with self._lock:
            record = self.get(error_id)
            if record is None:
                return {"status": "not_found", "message": "No diagnostic with that ID is available to this runtime."}
            invalid = {"status": "invalid_cursor", "message": "Restart this error ID and section without a cursor."}
            position, offset = 0, 0
            if cursor is not None:
                match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
                if match is None:
                    return invalid
                position, offset = int(match[2]), int(match[3])
                if not hmac.compare_digest(cursor, self._token(error_id, section, position, offset)):
                    return invalid
            segments = iter(self._segments(record, section, position))
            segment = next(segments, None)
            if segment is None:
                return invalid if cursor else {"status": "unavailable", "error_id": error_id, "section": section,
                                               "message": "This section was not captured in the retained diagnostic."}
            if cursor and (segment[0] != position or offset >= len(segment[1])):
                return invalid
            truncated = bool(record.get("record_truncated") or record.get("chain_truncated")
                             or section == "stderr" and record.get("context", {}).get("stderr_truncated"))
            text = ""

            def page(value, token):
                return {"status": "ok", "error_id": error_id, "section": section, "text": value,
                        "next_cursor": token, "source_truncated": truncated}

            while segment is not None:
                position, body = segment
                low, high = offset, min(len(body), offset + self.page_bytes)
                while low < high:
                    end = (low + high + 1) // 2
                    token = self._token(error_id, section, position, end)
                    if len(_json(page(text + body[offset:end], token)).encode("utf-8")) <= self.page_bytes:
                        low = end
                    else:
                        high = end - 1
                text += body[offset:low]
                if low < len(body):
                    return page(text, self._token(error_id, section, position, low))
                segment, offset = next(segments, None), 0
            return page(text, None)
