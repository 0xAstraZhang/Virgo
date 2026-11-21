"""Explicit, runtime-local access to bounded diagnostic evidence.

An error ID is a reference, not authority. Only ``remember`` grants access; neither
the active UI session nor a shared accounting store expands this collection. The
owner restores grants from its own canonical history. No model-supplied paths or
diagnostic log files are opened here.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import threading
import uuid
import zlib
from pathlib import Path

from ..diagnostics import capture_error, emergency_write, sanitize

SECTIONS = ("summary", "causes", "trace", "stderr", "attempts", "related")
MAX_RESPONSE_BYTES = 8192
MAX_RECORD_BYTES = 64 * 1024
_ERROR_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CURSOR = re.compile(r"([a-f0-9]{8}):([0-9]{1,19}):([0-9]{1,8})\Z")
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
    return {"error_id": error_id, "message": "Diagnostic evidence exceeded its retention budget.",
            "record_truncated": True, "context": {}}


class DiagnosticAccess:
    """In-memory runtime-scoped diagnostic snapshots. Only remember grants access."""

    def __init__(self, *, directory: Path | None = None, page_bytes: int = MAX_RESPONSE_BYTES):
        if type(page_bytes) is not int or not 512 <= page_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError(f"page_bytes must be between 512 and {MAX_RESPONSE_BYTES}")
        self.page_bytes = page_bytes
        self.directory = directory or Path(os.environ.get("VIRGO_HOME", Path.home() / ".virgo")) / "logs"
        self._lock = threading.RLock()
        self._closed = False
        self._version = 0
        self._next_seq = 0
        self._pending_sequence = 0
        self._instance_id = uuid.uuid4().hex[:8]
        # error_id -> (seq, call_key, payload_json)
        self._by_id: dict[str, tuple[int, str, str]] = {}
        # seq -> error_id
        self._by_seq: dict[int, str] = {}
        # call_key -> sorted list of seq
        self._by_call_key: dict[str, list[int]] = {}
        # error_id -> {"recorded": bool, "presented": bool, "pending": int | None}
        self._delivery: dict[str, dict[str, int | bool | None]] = {}

    def _ensure_open(self) -> None:
        if self._closed:
            raise OSError("Runtime diagnostic evidence has been closed")

    def _clear(self) -> None:
        self._by_id.clear()
        self._by_seq.clear()
        self._by_call_key.clear()
        self._delivery.clear()

    def close(self) -> None:
        try:
            with self._lock:
                self._closed = True
                self._clear()
        except Exception as error:
            # Cleanup is diagnostic bookkeeping, not the child operation's outcome.
            emergency_write(capture_error(error, phase="diagnostics.cleanup"))

    def remember(self, record: dict) -> dict | None:
        """Store a detached, filtered snapshot in this runtime's memory."""
        view = _record_view(record)
        if view is None:
            return None
        error_id = view["error_id"]
        payload = _json(view)
        call_key = _json([view["context"].get(key) for key in _CALL_KEYS])
        with self._lock:
            self._ensure_open()
            existing = self._by_id.get(error_id)
            if existing is None:
                self._next_seq += 1
                seq = self._next_seq
                self._by_id[error_id] = (seq, call_key, payload)
                self._by_seq[seq] = error_id
                self._by_call_key.setdefault(call_key, []).append(seq)
                self._version += 1
            else:
                seq, old_call_key, old_payload = existing
                if old_payload != payload or old_call_key != call_key:
                    if old_call_key != call_key:
                        old_list = self._by_call_key.get(old_call_key)
                        if old_list is not None:
                            idx = bisect.bisect_left(old_list, seq)
                            if idx < len(old_list) and old_list[idx] == seq:
                                old_list.pop(idx)
                        new_list = self._by_call_key.setdefault(call_key, [])
                        bisect.insort(new_list, seq)
                    self._by_id[error_id] = (seq, call_key, payload)
                    self._version += 1
        return view

    def get(self, error_id: str) -> dict | None:
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return None
        with self._lock:
            self._ensure_open()
            entry = self._by_id.get(error_id)
            return json.loads(entry[2]) if entry is not None else None

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            self._ensure_open()
            if not self._by_seq or limit <= 0:
                return []
            seqs = list(self._by_seq.keys())[-limit:]
            return [json.loads(self._by_id[self._by_seq[seq]][2]) for seq in seqs]

    def mark(self, error_id: str, action: str) -> bool:
        """Delivery bookkeeping; cannot create evidence grants."""
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return False
        with self._lock:
            self._ensure_open()
            state = self._delivery.setdefault(
                error_id, {"recorded": False, "presented": False, "pending": None},
            )
            if action == "record":
                if state["recorded"]:
                    return False
                state["recorded"] = True
                return True
            if action == "presented":
                if state["presented"]:
                    return False
                state["presented"] = True
                state["pending"] = None
                return True
            if action == "pending":
                if state["presented"] or state["pending"] is not None:
                    return False
                self._pending_sequence += 1
                state["pending"] = self._pending_sequence
                return True
            raise ValueError("Unknown diagnostic delivery action")

    def pending(self, limit: int = 3) -> list[str]:
        with self._lock:
            self._ensure_open()
            items = [
                (int(state["pending"]), err_id)
                for err_id, state in self._delivery.items()
                if state["pending"] is not None
            ]
            items.sort(key=lambda pair: pair[0])
            return [err_id for _, err_id in items[: max(0, limit)]]

    @staticmethod
    def _summary(record: dict) -> dict:
        result = {key: record[key] for key in _SUMMARY_FIELDS if key in record}
        result["context"] = {key: value for key, value in record.get("context", {}).items() if key != "stderr_tail"}
        result["cause_count"] = len(record.get("cause_chain", []))
        return result

    def _segments(self, record, section, position):
        """Resume at a sequence position without scanning all earlier rows."""
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
            seq_list = self._by_call_key.get(key, [])
            if not seq_list:
                return
            first = seq_list[0]
            if position == 0:
                yield 0, '{"scope":"retained_authorized_failures","records":['
            start_idx = bisect.bisect_left(seq_list, max(1, position))
            for seq in seq_list[start_idx:]:
                payload = self._by_id[self._by_seq[seq]][2]
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

    def _token(self, error_id: str, section: str, position: int, offset: int) -> str:
        scope = f"{self._instance_id}\0{self._version}\0{error_id}\0{section}".encode()
        tag = zlib.crc32(scope) & 0xFFFFFFFF
        return f"{tag:08x}:{position}:{offset}"

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
                if cursor != self._token(error_id, section, position, offset):
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
