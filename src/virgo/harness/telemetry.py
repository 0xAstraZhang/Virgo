"""Persist model-call metadata without putting internal requests in model history."""

from __future__ import annotations

import time
from dataclasses import asdict

from ..diagnostics import safe_text, sanitize


def record_call(store, model, message, *, purpose, started, effort=None, agent="main", tool_call_id=None,
                duration=None, request_metadata=None, call_metadata=None):
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
    if call_metadata is not None:
        data["review_context"] = call_metadata
    return store.append("custom", kind="model_call", data=data)["id"]
