"""Persist model-call metadata without putting internal requests in model history."""

from __future__ import annotations

import time
from dataclasses import asdict


def record_call(store, model, message, *, purpose, started, effort=None, agent="main", tool_call_id=None,
                duration=None):
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
    return store.append("custom", kind="model_call", data=data)["id"]
