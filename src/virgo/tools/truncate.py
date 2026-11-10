"""Output budgets: every tool result is bounded before it enters the conversation.

When output is cut, the full text is saved under ``.virgo/outputs/`` and the model is told
where to find it, so nothing is lost and the context stays small.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
_counter = itertools.count(1)


def _cut_bytes(text: str, max_bytes: int, from_end: bool) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    data = data[-max_bytes:] if from_end else data[:max_bytes]
    return data.decode("utf-8", errors="ignore")


def truncate_head(text: str, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    cut = len(lines) > max_lines
    out = "".join(lines[:max_lines])
    if len(out.encode("utf-8")) > max_bytes:
        out = _cut_bytes(out, max_bytes, from_end=False)
        out = out[: out.rfind("\n") + 1] or out
        cut = True
    return out, cut


def truncate_tail(text: str, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    cut = len(lines) > max_lines
    out = "".join(lines[-max_lines:]) if cut else text
    if len(out.encode("utf-8")) > max_bytes:
        out = _cut_bytes(out, max_bytes, from_end=True)
        nl = out.find("\n")
        out = out[nl + 1 :] if 0 <= nl < len(out) - 1 else out
        cut = True
    return out, cut


def outputs_dir(cwd: Path) -> Path:
    d = Path(cwd) / ".virgo" / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_output(text: str, cwd: Path, prefix: str) -> Path:
    path = outputs_dir(cwd) / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{next(_counter)}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def budget(text: str, cwd: Path, prefix: str, keep: str = "head") -> str:
    """Bound `text`; if cut, save the full text and append a pointer to it."""
    if len(text.encode("utf-8")) <= MAX_BYTES and text.count("\n") < MAX_LINES:
        return text
    fn = truncate_tail if keep == "tail" else truncate_head
    shown, _ = fn(text)
    path = save_output(text, cwd, prefix)
    total = text.count("\n") + 1
    where = "last" if keep == "tail" else "first"
    return (
        f"{shown.rstrip()}\n\n[Output truncated: showing the {where} {shown.count(chr(10)) + 1} of {total} lines. "
        f"Full output saved to {path} — use read with offset/limit or grep to inspect it.]"
    )
