"""Runtime-owned diagnostic references, delivered at natural request boundaries.

This journal records evidence and delivery, never decides whether a task should
continue or whether an operation may be replayed.
"""

from __future__ import annotations

import threading

from ..ai.types import UserMessage
from ..diagnostics import emergency_write
from .diagnostic_access import DiagnosticAccess
from .diagnostic_view import diagnostic_summary
from .store import SessionSaveError


def message_diagnostics(message) -> list[dict]:
    """Only explicit metadata grants access; never parse IDs from message text."""
    if isinstance(message, dict):
        details = message.get("details")
        error = message.get("error_details")
    else:
        details = getattr(message, "details", None)
        error = getattr(message, "error_details", None)
    records = [error] if isinstance(error, dict) else []
    if isinstance(details, dict):
        if isinstance(details.get("error"), dict):
            records.append(details["error"])
        records.extend(record for record in details.get("related_errors", []) if isinstance(record, dict))
    return records


class RuntimeDiagnostics:
    def __init__(self, runtime, messages=()):
        self.runtime = runtime
        self.access = DiagnosticAccess()
        self.scope = f"{runtime.role}:{runtime.name}"
        self._checkpoint = False
        self._lock = threading.RLock()
        store = runtime.store
        if store is not None:
            # This runtime's own branch is an explicit grant. An accounting
            # store shared by children is deliberately not used here.
            for entry in store.branch():
                if entry.get("type") == "message":
                    for record in message_diagnostics(entry.get("message", {})):
                        self.safely("_restore_record", record)
                elif entry.get("kind") == "runtime_diagnostic":
                    data = entry.get("data", {})
                    if data.get("scope") != self.scope:
                        continue
                    action, error_id = data.get("action"), data.get("error_id")
                    if action == "record" and isinstance(data.get("record"), dict):
                        self.safely("_restore_record", data["record"])
                    elif action in ("pending", "presented"):
                        self.safely("_restore_delivery", error_id, action)
            compacted = store.last_compaction()
            if compacted is not None:
                self._checkpoint = not any(
                    item.get("scope") == self.scope and item.get("compaction_id") == compacted["id"]
                    for item in store.custom("diagnostic_checkpoint")
                )
        for message in messages:
            for record in message_diagnostics(message):
                self.safely("_restore_record", record)

    def _restore_record(self, record):
        saved = self.access.remember(record)
        if saved is not None:
            self.access.mark(saved["error_id"], "record")

    def _restore_delivery(self, error_id, action):
        self.access.mark(error_id, action)

    def _persist(self, **data):
        store = self.runtime.store
        if store is None or store.save_error is not None:
            return
        try:
            store.append("custom", kind="runtime_diagnostic", data={"scope": self.scope, **data})
        except SessionSaveError as error:
            self.runtime.session.report_save_error(error)

    def remember(self, record):
        """Grant a reference explicitly propagated to this runtime, once."""
        saved = self.access.remember(record)
        if saved is not None and self.access.mark(saved["error_id"], "record"):
            # Database locks are released before callbacks into SessionStore.
            self._persist(action="record", error_id=saved["error_id"], record=saved)

    def queue(self, record):
        """Queue facts without waking an agent or creating an extra model turn."""
        self.remember(record)
        error_id = record.get("error_id")
        if record.get("kind") != "aborted" and self.access.get(error_id) is not None:
            if self.access.mark(error_id, "pending"):
                self._persist(action="pending", error_id=error_id)

    def presented(self, records):
        for record in records:
            self.remember(record)
            error_id = record.get("error_id")
            if self.access.get(error_id) is not None and self.access.mark(error_id, "presented"):
                self._persist(action="presented", error_id=error_id)

    def after_compaction(self):
        with self._lock:
            self._checkpoint = True

    def _append(self, text):
        runtime = self.runtime
        message = UserMessage(f'<notification source="diagnostics">\n{text}\n</notification>', kind="notification")
        runtime.agent.append_message(message)

    def before_request(self):
        """Append evidence only when a model request is already going to happen."""
        if not self.runtime.can_run() or self.runtime.agent.signal.is_set():
            return
        query_tool = self.runtime.diagnostic_query_tool_name()
        with self._lock:
            checkpoint = self._checkpoint
            self._checkpoint = False
        records, unavailable = [], []
        while len(records) < 3:
            pending = self.access.pending(limit=3)
            if not pending:
                break
            for error_id in pending:
                record = self.access.get(error_id)
                if record is None:
                    self.access.mark(error_id, "presented")
                    self._persist(action="presented", error_id=error_id)
                    if len(unavailable) < 3:
                        unavailable.append(error_id)
                else:
                    records.append(record)
            if records:
                break
        if unavailable:
            self._append("Diagnostic evidence is no longer retained for: " + ", ".join(unavailable) +
                         ". The original outcomes have not been reclassified.")
        if records and self.runtime.can_run():
            self._append("Runtime failure observations:\n" + "\n\n".join(
                diagnostic_summary(record, max_bytes=1536, query_tool=query_tool) for record in records
            ))
            self.presented(records)
        if checkpoint:
            recent = self.access.recent(limit=3)
            if recent and self.runtime.can_run():
                lines = [
                    "Retained diagnostic references from earlier work. Check current state before acting; "
                    "these records may describe attempts that have already recovered."
                ]
                lines.extend(
                    f"{record['error_id']}: {record.get('phase', 'unknown')} / {record.get('code', 'ERROR')}"
                    for record in recent
                )
                if query_tool:
                    lines.append(f"Use {query_tool}(error_id, section) for scoped evidence.")
                self._append("\n".join(lines))
                store = self.runtime.store
                if store is not None and store.save_error is None:
                    compacted = store.last_compaction()
                    try:
                        store.append("custom", kind="diagnostic_checkpoint", data={
                            "scope": self.scope,
                            "compaction_id": compacted["id"] if compacted is not None else None,
                        })
                    except SessionSaveError as error:
                        self.runtime.session.report_save_error(error)

    def safely(self, method, *args):
        """Failure in the evidence path must not replace an execution result."""
        try:
            return getattr(self, method)(*args)
        except Exception as error:
            from ..diagnostics import capture_error

            emergency_write(capture_error(error, phase="diagnostics.delivery", context={
                "agent": self.runtime.name, "runtime_id": self.runtime.diagnostic_id,
            }))
            return None
