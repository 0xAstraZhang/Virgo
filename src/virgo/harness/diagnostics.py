"""Failure-isolated, indexed diagnostics independent of conversation persistence."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from ..diagnostics import capture_error, emergency_write, safe_text, sanitize
from .diagnostic_store import connect

_ERROR_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_UNSAVED_SEQUENCE = 2**63 - 1
_LEGACY = re.compile(r"harness-[\w-]+\.jsonl(?:\.\d+)?\Z")


def _json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _occurred(record, fallback_time=None):
    try:
        timestamp = datetime.fromisoformat(record.get("timestamp"))
        timestamp = timestamp.replace(tzinfo=timestamp.tzinfo or UTC).astimezone(UTC)
    except (ValueError, TypeError):
        timestamp = fallback_time or datetime.now(UTC)
    return timestamp.isoformat()


def _fallback_record(details, error) -> dict:
    raw = details if issubclass(type(details), dict) else {}
    record = capture_error(safe_text(dict.get(raw, "message", "Diagnostic metadata could not be read.")),
                           phase=safe_text(dict.get(raw, "phase", "diagnostics.record")))
    error_id = dict.get(raw, "error_id")
    if isinstance(error_id, str) and _ERROR_ID.fullmatch(error_id):
        record["error_id"] = error_id
    record["logging_error"] = safe_text(error)
    return record


def _warn(warnings, message):
    if len(warnings) < 10:
        warnings.append(message)
    elif len(warnings) == 10:
        warnings.append("Additional import warnings omitted.")


class DiagnosticLog:
    """All errors in one durable store; only unsaved emergency records live in RAM."""

    def __init__(self, directory: Path):
        self.path = Path(directory) / "diagnostics.sqlite3"
        self.instance = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._connection = None
        self._failed: OrderedDict[str, dict] = OrderedDict()

    def _db(self):
        if self._connection is None:
            db = connect(self.path, durable=True)
            try:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS errors (
                        seq INTEGER PRIMARY KEY, error_id TEXT NOT NULL UNIQUE,
                        occurred TEXT NOT NULL, instance TEXT NOT NULL, payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS errors_time ON errors(occurred DESC, seq DESC);
                    CREATE INDEX IF NOT EXISTS errors_instance ON errors(instance, seq);
                    CREATE TABLE IF NOT EXISTS imports (
                        path TEXT PRIMARY KEY, identity TEXT NOT NULL, offset INTEGER NOT NULL
                    );
                """)
            except BaseException:
                db.close()
                raise
            self._connection = db
        return self._connection

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __del__(self):
        if getattr(self, "_connection", None) is not None:
            self._connection.close()

    def _insert(self, db, record, *, instance, fallback_time=None):
        record = dict(record)
        record.pop("log_pattern", None)
        record.pop("log_expired", None)
        record["log_path"] = str(self.path)
        db.execute("INSERT OR IGNORE INTO errors(error_id, occurred, instance, payload) VALUES (?, ?, ?, ?)",
                   (record["error_id"], _occurred(record, fallback_time), instance, _json(record)))
        return record

    def record(self, details: dict) -> dict:
        """Never replace the original failure, even when storage itself is broken."""
        with self._lock:
            record = None
            try:
                record = sanitize(details)
                if not isinstance(record, dict):
                    raise TypeError("diagnostic record must be an object")
                error_id = record.get("error_id")
                if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
                    error_id = record["error_id"] = uuid.uuid4().hex
                    record["record_warning"] = "Missing or invalid error ID was replaced."
                db = self._db()
                row = db.execute("SELECT payload FROM errors WHERE error_id=?", (error_id,)).fetchone()
                if row is None:
                    self._insert(db, record, instance=self.instance)
                    row = db.execute("SELECT payload FROM errors WHERE error_id=?", (error_id,)).fetchone()
                self._failed.pop(error_id, None)
                return json.loads(row[0])
            except Exception as error:
                if not isinstance(record, dict):
                    record = _fallback_record(details, error)
                if not isinstance(record.get("error_id"), str) or not _ERROR_ID.fullmatch(record["error_id"]):
                    record["error_id"] = uuid.uuid4().hex
                previous = self._failed.get(record["error_id"])
                if previous is not None:
                    return previous
                record.pop("log_path", None)
                record.pop("log_pattern", None)
                record["logging_error"] = safe_text(error)
                record["timestamp"] = _occurred(record)
                emergency_write(record, error)
                self._failed[record["error_id"]] = record
                while len(self._failed) > 100:
                    self._failed.popitem(last=False)
                return record

    def recent(self, limit: int = 100) -> list[dict]:
        """Bounded presentation of this instance, not a retention policy."""
        with self._lock:
            saved = []
            if self.path.exists():
                saved = [json.loads(row[0]) for row in self._db().execute(
                    "SELECT payload FROM errors WHERE instance=? ORDER BY seq DESC LIMIT ?", (self.instance, limit))]
                saved.reverse()
            return (saved + list(self._failed.values()))[-limit:]

    def initial_records(self):
        """Stream only this owner's initialization failures, never historical grants."""
        after = 0
        while self.path.exists():
            try:
                with self._lock:
                    row = self._db().execute(
                        "SELECT seq, payload FROM errors WHERE instance=? AND seq>? ORDER BY seq LIMIT 1",
                        (self.instance, after)).fetchone()
            except (OSError, sqlite3.Error):
                break  # A broken diagnostic store must not prevent session startup.
            if row is None:
                break
            after = row[0]
            yield json.loads(row[1])
        yield from list(self._failed.values())

    def lookup(self, query: str) -> dict | None:
        if not _ERROR_ID.fullmatch(query):
            return None
        with self._lock:
            if query in self._failed:
                return self._failed[query]
            if not self.path.exists():
                return None
            db = self._db()
            row = db.execute("SELECT payload FROM errors WHERE error_id=?", (query,)).fetchone()
            if row:
                return json.loads(row[0])
            # The validated ASCII alphabet makes this a literal, indexed prefix range.
            upper = query[:-1] + chr(ord(query[-1]) + 1)
            rows = db.execute("SELECT payload FROM errors WHERE error_id>=? AND error_id<? LIMIT 2",
                              (query, upper)).fetchall()
            matches = [json.loads(row[0]) for row in rows]
            matches.extend(value for key, value in self._failed.items() if key.startswith(query))
            return matches[0] if len(matches) == 1 else None

    def history_page(self, before: str | None = None, *, limit: int = 10) -> dict:
        """Merge bounded emergency evidence with indexed durable history."""
        with self._lock:
            anchor = None
            failed_anchor = None
            if before is not None:
                if isinstance(before, str) and before.startswith("failed:"):
                    failed_anchor = before.removeprefix("failed:")
                    record = self._failed.get(failed_anchor)
                    if record is None:
                        raise ValueError("Invalid diagnostic history cursor")
                    anchor = (record["timestamp"], _UNSAVED_SEQUENCE, failed_anchor)
                elif not isinstance(before, str) or not re.fullmatch(r"[0-9]{1,19}", before):
                    raise ValueError("Invalid diagnostic history cursor")
            rows, stored_ids = [], set()
            storage_unavailable = False
            try:
                db = self._db() if self.path.exists() else None
                if before is not None and failed_anchor is None:
                    found = db.execute("SELECT occurred, seq FROM errors WHERE seq=?", (before,)).fetchone() if db else None
                    if found is None:
                        raise ValueError("Invalid diagnostic history cursor")
                    anchor = (*found, "")
                if db is not None:
                    if self._failed:
                        placeholders = ",".join("?" for _ in self._failed)
                        stored_ids = {row[0] for row in db.execute(
                            f"SELECT error_id FROM errors WHERE error_id IN ({placeholders})", tuple(self._failed))}
                        if failed_anchor in stored_ids:
                            raise ValueError("Emergency cursor was persisted; restart diagnostic history")
                    where = "WHERE (occurred, seq) < (?, ?)" if anchor else ""
                    params = anchor[:2] if anchor else ()
                    rows = db.execute(
                        f"SELECT seq, occurred, payload FROM errors {where} ORDER BY occurred DESC, seq DESC LIMIT ?",
                        (*params, limit + 1)).fetchall()
            except (OSError, sqlite3.Error):
                # A broken database must not hide the emergency records it caused.
                # Durable cursors cannot be resolved without their database anchor.
                if before is not None and failed_anchor is None:
                    raise
                storage_unavailable = True
                rows, stored_ids = [], set()
            items = [((occurred, seq, ""), str(seq), json.loads(payload)) for seq, occurred, payload in rows]
            stored_ids.update(item[2]["error_id"] for item in items)
            for error_id, record in self._failed.items():
                key = (record["timestamp"], _UNSAVED_SEQUENCE, error_id)
                if error_id not in stored_ids and (anchor is None or key < anchor):
                    items.append((key, f"failed:{error_id}", record))
            items.sort(key=lambda item: item[0], reverse=True)
            return {"records": [item[2] for item in items[:limit]],
                    "next_cursor": items[limit - 1][1] if len(items) > limit else None,
                    "storage_unavailable": storage_unavailable}

    def import_legacy(self) -> list[str]:
        """Import complete lines in resumable batches, without editing the source files."""
        if not self.path.parent.exists():
            return []
        warnings = []
        for path in sorted(self.path.parent.iterdir()):
            if not _LEGACY.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                self._import_file(path, warnings)
            except Exception as error:
                _warn(warnings, f"{path.name}: {safe_text(error)}")
        return warnings

    def _import_file(self, path, warnings):
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            identity = f"{info.st_dev}:{info.st_ino}"
            fallback_time = datetime.fromtimestamp(info.st_mtime, UTC)
            while True:
                with self._lock:
                    db = self._db()
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        saved = db.execute("SELECT identity, offset FROM imports WHERE path=?", (path.name,)).fetchone()
                        offset = saved[1] if saved and saved[0] == identity and saved[1] <= os.fstat(source.fileno()).st_size else 0
                        source.seek(offset)
                        finished = False
                        for _ in range(100):
                            line = source.readline(4 * 1024 * 1024)
                            if not line or not line.endswith(b"\n"):
                                if len(line) == 4 * 1024 * 1024:
                                    while line and not line.endswith(b"\n"):
                                        line = source.readline(4 * 1024 * 1024)
                                    if line:
                                        offset = source.tell()
                                        _warn(warnings, f"{path.name}: oversized record skipped")
                                        continue
                                finished = True
                                break
                            offset = source.tell()
                            try:
                                record = sanitize(json.loads(line))
                                if not isinstance(record, dict) or not isinstance(record.get("error_id"), str) or not _ERROR_ID.fullmatch(record["error_id"]):
                                    raise ValueError("invalid diagnostic record")
                                self._insert(db, record, instance="legacy", fallback_time=fallback_time)
                            except (ValueError, TypeError, UnicodeError) as error:
                                _warn(warnings, f"{path.name} at byte {offset}: {safe_text(error)}")
                        db.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?)", (path.name, identity, offset))
                        db.execute("COMMIT")
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                if finished:
                    break
