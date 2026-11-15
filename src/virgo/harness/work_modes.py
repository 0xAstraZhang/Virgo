"""Work intent, independent of approval strategy and platform isolation."""
from __future__ import annotations

WORK_MODES = ("default", "plan")


def normalize_work_mode(mode: str) -> str:
    if not isinstance(mode, str) or mode not in WORK_MODES:
        raise ValueError(f"workMode must be one of {', '.join(WORK_MODES)}")
    return mode
