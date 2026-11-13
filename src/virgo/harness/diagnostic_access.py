"""Explicit, runtime-local access to bounded diagnostic evidence.

An error ID is a reference, not authority. Only ``remember`` grants access; neither
the active UI session nor a shared accounting store expands this collection. The
owner restores grants from its own canonical history. No model-supplied paths or
diagnostic log files are opened here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections import OrderedDict

from ..diagnostics import sanitize

SECTIONS = ("summary", "causes", "trace", "stderr", "attempts", "related")
MAX_RESPONSE_BYTES = 8192
MAX_RECORD_BYTES = 64 * 1024
_ERROR_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CURSOR = re.compile(r"([a-f0-9]{24}):([0-9]{1,8})\Z")
_RECORD_FIELDS = (
    "schema_version", "error_id", "timestamp", "code", "phase", "kind", "message", "exception_type",
    "traceback", "chain_truncated", "record_truncated", "log_expired", "logging_error", "record_warning",
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
    """A bounded collection of explicit grants, with tombstones for expired records."""

    def __init__(self, *, max_records: int = 100, max_ids: int = 1000, page_bytes: int = MAX_RESPONSE_BYTES):
        if type(max_records) is not int or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        if type(max_ids) is not int or max_ids < max_records:
            raise ValueError("max_ids must be an integer at least as large as max_records")
        if type(page_bytes) is not int or not 512 <= page_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError(f"page_bytes must be between 512 and {MAX_RESPONSE_BYTES}")
        self.max_records, self.max_ids, self.page_bytes = max_records, max_ids, page_bytes
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._grants: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.RLock()

    def remember(self, record: dict) -> None:
        """Grant exactly this error ID and retain a detached, filtered snapshot."""
        view = _record_view(record)
        if view is None:
            return
        error_id = view["error_id"]
        with self._lock:
            self._grants[error_id] = None
            self._records[error_id] = view
            while len(self._records) > self.max_records:
                self._records.popitem(last=False)
            while len(self._grants) > self.max_ids:
                expired, _ = self._grants.popitem(last=False)
                self._records.pop(expired, None)

    def get(self, error_id: str) -> dict | None:
        """Read an exact, explicitly granted ID; never fall back to a similar error."""
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return None
        with self._lock:
            return copy.deepcopy(self._records.get(error_id))

    def recent(self) -> list[dict]:
        """Return retained snapshots without granting or reading any other scope."""
        with self._lock:
            return copy.deepcopy(list(self._records.values()))

    @staticmethod
    def _summary(record: dict) -> dict:
        result = {key: record[key] for key in _SUMMARY_FIELDS if key in record}
        result["context"] = {key: value for key, value in record.get("context", {}).items()
                             if key != "stderr_tail"}
        result["cause_count"] = len(record.get("cause_chain", []))
        return result

    def _section(self, record: dict, section: str) -> str | None:
        if section == "summary":
            return _json(self._summary(record))
        if section == "causes":
            causes = record.get("cause_chain")
            return _json(causes) if causes else None
        if section == "trace":
            trace = record.get("traceback")
            return trace if isinstance(trace, str) and trace else None
        if section == "stderr":
            stderr = record.get("context", {}).get("stderr_tail")
            return stderr if isinstance(stderr, str) and stderr else None
        if section == "attempts":
            context = record.get("context", {})
            if not context.get("model_call_id"):
                return None
            keys = ("model_call_id", "runtime_id", "session_id", "run_id", "agent", "purpose")
            records = [self._summary(item) for item in self._records.values()
                       if all(item.get("context", {}).get(key) == context.get(key) for key in keys)]
            return _json({"scope": "retained_authorized_failures", "records": records})
        if section == "related":
            records = []
            for reference in record.get("related_error_ids", []):
                if reference in self._grants:
                    related = self._records.get(reference)
                    records.append(self._summary(related) if related else {"error_id": reference, "status": "expired"})
            return _json({"scope": "explicitly_authorized_references", "records": records})
        return None

    def read(self, error_id: str, section: str = "summary", cursor: str | None = None) -> dict:
        """Return a bounded JSON envelope; concatenate ``text`` pages for one section.

        Cursors are bound to the error, section and evidence snapshot. If evidence
        changes between pages the caller must restart, rather than silently mix it.
        """
        if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
            return {"status": "not_found", "message": "No diagnostic with that ID is available to this runtime."}
        if section not in SECTIONS:
            return {"status": "invalid_section", "message": "Choose summary, causes, trace, stderr, attempts or related."}
        with self._lock:
            if error_id not in self._grants:
                return {"status": "not_found", "message": "No diagnostic with that ID is available to this runtime."}
            record = self._records.get(error_id)
            if record is None:
                return {"status": "expired", "error_id": error_id,
                        "message": "This runtime's retained evidence has expired; no substitute record was read."}
            body = self._section(record, section)
            truncated = bool(record.get("record_truncated") or record.get("chain_truncated")
                             or section == "stderr" and record.get("context", {}).get("stderr_truncated"))
        if body is None:
            return {"status": "unavailable", "error_id": error_id, "section": section,
                    "message": "This section was not captured in the retained diagnostic."}
        digest = hashlib.sha256(f"{error_id}\0{section}\0{body}".encode()).hexdigest()[:24]
        start = 0
        if cursor is not None:
            match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
            if match is None or match[1] != digest or not 0 < int(match[2]) < len(body):
                return {"status": "invalid_cursor", "message": "Restart this error ID and section without a cursor."}
            start = int(match[2])

        def page(end):
            return {"status": "ok", "error_id": error_id, "section": section, "text": body[start:end],
                    "next_cursor": f"{digest}:{end}" if end < len(body) else None,
                    "source_truncated": truncated}

        low, high = start, min(len(body), start + self.page_bytes)
        while low < high:
            end = (low + high + 1) // 2
            if len(_json(page(end)).encode("utf-8")) <= self.page_bytes:
                low = end
            else:
                high = end - 1
        return page(low)
