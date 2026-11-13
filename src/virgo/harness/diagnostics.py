"""Failure-isolated JSONL diagnostics, independent of conversation persistence."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from ..diagnostics import capture_error, emergency_write, safe_text, sanitize

_ERROR_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _json_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"


def _fallback_record(details, error) -> dict:
    """Recover named primitive fields even if a custom record mapping is broken."""
    raw = details if issubclass(type(details), dict) else {}
    phase = safe_text(dict.get(raw, "phase", "diagnostics.record"))
    message = safe_text(dict.get(raw, "message", "Diagnostic metadata could not be read."))
    record = capture_error(message, phase=phase)
    error_id = dict.get(raw, "error_id")
    if isinstance(error_id, str) and _ERROR_ID.fullmatch(error_id):
        record["error_id"] = error_id
    record["logging_error"] = safe_text(error)
    return record


class DiagnosticLog:
    """One private log per session owner, with bounded size and retained history.

    The last 100 records are deduplicated by error ID. No SessionStore callbacks are involved,
    so even a broken conversation file can be diagnosed without recursion.
    """

    def __init__(self, directory: Path, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 2):
        if type(max_bytes) is not int or max_bytes < 512:
            raise ValueError("diagnostic max_bytes must be an integer of at least 512")
        if type(backups) is not int or backups < 0:
            raise ValueError("diagnostic backups must be a non-negative integer")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.path = Path(directory) / f"harness-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl"
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.RLock()
        self._records: OrderedDict[str, dict] = OrderedDict()

    def record(self, details: dict) -> dict:
        """Return the saved record (or an explicit stderr fallback) without raising."""
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
                if error_id in self._records:
                    return self._records[error_id]
                record["log_path"] = str(self.path)
                # Old copies of a record may outlive a rotation. This stable
                # family pattern remains useful alongside the error ID.
                record["log_pattern"] = str(self.path) + "*"
                record, line = self._bounded_line(record)
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._rotate(len(line))  # ensure_ascii makes character and byte counts identical.
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
                    written = handle.write(line)
                    if written != len(line):
                        raise OSError("short diagnostic write")
                    handle.flush()
                record["log_path"] = str(self.path)
                record["log_pattern"] = str(self.path) + "*"
            except Exception as error:
                if not isinstance(record, dict):
                    record = _fallback_record(details, error)
                if not isinstance(record.get("error_id"), str) or not _ERROR_ID.fullmatch(record["error_id"]):
                    record["error_id"] = uuid.uuid4().hex
                record.pop("log_path", None)
                record["logging_error"] = safe_text(error)
                emergency_write(record, error)
            self._records[record["error_id"]] = record
            while len(self._records) > 100:
                self._records.popitem(last=False)
            return record

    def _bounded_line(self, record: dict) -> tuple[dict, str]:
        """Preserve ordinary traces; use an explicit cause summary for an oversized record."""
        line = _json_line(record)
        if len(line) <= self.max_bytes:
            return record, line
        chain = record.get("cause_chain")
        causes = [node for node in chain if isinstance(node, dict)] if isinstance(chain, list) else []
        if causes and causes[0].get("relation") == "error":
            causes = causes[1:]  # The outer exception already has its own type and message above.
        causes = causes[:1] + causes[-1:] if len(causes) > 1 else causes

        def clip(value, limit):
            value = safe_text(value)
            if len(json.dumps(value, ensure_ascii=True)) - 2 <= limit:
                return value
            low, high = 0, len(value)
            while low < high:
                middle = (low + high + 1) // 2
                size = len(json.dumps(value[:middle] + "[...]", ensure_ascii=True)) - 2
                if size <= limit:
                    low = middle
                else:
                    high = middle - 1
            return value[:low] + "[...]"

        for limit in (512, 256, 128, 64, 32, 16, 8):
            summary = {
                "error_id": record["error_id"],
                "phase": clip(record.get("phase", "unknown"), min(limit, 64)),
                "message": clip(record.get("message", ""), limit),
                "exception_type": clip(record.get("exception_type") or "", min(limit, 64)),
                "cause_chain": [{
                    "exception_type": clip(cause.get("exception_type") or "", min(limit, 64)),
                    "message": clip(cause.get("message", ""), min(limit, 128)),
                } for cause in causes],
                "record_truncated": True,
                "original_bytes": len(line),
            }
            if limit >= 64:
                summary.update({key: record[key] for key in ("schema_version", "timestamp", "kind") if key in record})
                summary["code"] = clip(record.get("code", "ERROR"), 64)
                context = record.get("context")
                summary["context"] = {
                    key: clip(value, 64) for key, value in (context if isinstance(context, dict) else {}).items()
                    if key in {"session_id", "run_id", "agent", "tool_call_id", "model_call_id"}
                }
            bounded = _json_line(summary)
            if len(bounded) <= self.max_bytes:
                return summary, bounded
        # A valid ID plus the shortest summary fits the constructor's minimum.
        raise ValueError("diagnostic summary exceeds the configured log budget")

    def _rotate(self, incoming: int) -> None:
        if not self.path.exists() or self.path.stat().st_size + incoming <= self.max_bytes:
            return
        if not self.backups:
            self.path.unlink()
            self._move_cached_path(self.path, None)
            return
        for number in range(self.backups, 0, -1):
            target = self.path.with_suffix(f".jsonl.{number}")
            source = self.path if number == 1 else self.path.with_suffix(f".jsonl.{number - 1}")
            if source.exists():
                os.replace(source, target)
                if number == self.backups:
                    self._move_cached_path(target, None)
                self._move_cached_path(source, target)

    def _move_cached_path(self, source: Path, target: Path | None) -> None:
        for record in self._records.values():
            if record.get("log_path") != str(source):
                continue
            if target is None:
                record.pop("log_path", None)
                record["log_expired"] = True
            else:
                record["log_path"] = str(target)

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self._records.values())
