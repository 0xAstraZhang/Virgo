"""Provider retry timing; all attempts still belong to Runtime._attempts."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after_seconds(details: dict | None, *, now: float | None = None) -> float | None:
    context = details.get("context", {}) if isinstance(details, dict) else {}
    http = context.get("http", {}) if isinstance(context, dict) else {}
    value = http.get("retry_after") if isinstance(http, dict) else None
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = max(0.0, date.timestamp() - (datetime.now(UTC).timestamp() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
