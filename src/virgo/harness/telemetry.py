"""Persist model-call metadata without putting internal requests in model history."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict

from ..diagnostics import safe_text, sanitize


def record_call(store, model, message, *, purpose, started, effort=None, agent="main", tool_call_id=None,
                duration=None, request_metadata=None):
    data = {
        "purpose": purpose,
        "agent": agent,
        "tool_call_id": tool_call_id,
        "model": f"{message.provider}/{message.model}" if message.provider and message.model else model.spec,
        "effort": effort,
        "duration": round(time.monotonic() - started if duration is None else duration, 6),
        "stop_reason": message.stop_reason,
        "usage": asdict(message.usage),
    }
    if request_metadata is not None:
        data.update(request_metadata)
    if message.error_kind or message.error_message or message.error_details:
        data["error_kind"] = message.error_kind
        data["error_message"] = safe_text(message.error_message) if message.error_message else None
        if message.error_details:
            data["error_details"] = sanitize(message.error_details)
            data["error_id"] = message.error_details.get("error_id")
            context = message.error_details.get("context") or {}
            for key in ("run_id", "model_call_id", "attempt", "max_retries"):
                if context.get(key) is not None:
                    data[key] = context[key]
    return store.append("custom", kind="model_call", data=data)["id"]

class RunTelemetry:
    """In-process request observations, independent of saved conversation branches."""

    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._calls = {}
        self._tool_calls = 0
        self._tool_results = 0
        self._tool_executions = 0
        self._unknown_tools = 0

    def observer(self, purpose, agent):
        def observe(event):
            data = dict(event)
            phase = data.pop("phase")
            with self._lock:
                if phase == "start":
                    self._calls[data["id"]] = {**data, "purpose": purpose, "agent": agent}
                else:
                    self._calls[data["id"]].update(data)
        return observe

    def tool_event(self, event):
        with self._lock:
            if event.type == "tool_execution_start":
                self._tool_calls += 1
            elif event.type == "tool_execution_end":
                self._tool_results += 1
                result = event.result
                details = result.details if result is not None else None
                execution = details.get("execution", {}) if isinstance(details, dict) else {}
                started = execution.get("started")
                if started is True:
                    self._tool_executions += 1
                elif started is not False:
                    if result is not None and not result.is_error and not isinstance(details, dict):
                        self._tool_executions += 1
                    else:
                        self._unknown_tools += 1

    def snapshot(self, *, complete, setup_s, execution_s):
        with self._lock:
            calls = [dict(call) for call in self._calls.values()]
            complete = (complete and all("stop_reason" in call for call in calls)
                        and self._tool_calls == self._tool_results)
            return {
                "type": "run_telemetry", "schema_version": 1, "run_id": self.run_id,
                "complete": complete, "model_calls": calls,
                "tool_calls": self._tool_calls if complete else None,
                "tool_executions": self._tool_executions if complete and not self._unknown_tools else None,
                "agent_setup_s": setup_s, "agent_execution_s": execution_s,
            }
