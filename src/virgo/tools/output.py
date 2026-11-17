"""One implementation for bounded tool output and spilled full output.

Every tool result is bounded before it enters the conversation. When text is
cut, the full text is written under `.virgo/outputs/` and the model is told
where to find it. Long commands spool to the same directory while they run, so
only a bounded tail is ever decoded and the context stays small.
"""

from __future__ import annotations

import itertools
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..i18n import Translator

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
_counter = itertools.count(1)


@dataclass(frozen=True)
class CommandOutput:
    text: str
    path: Path | None = None
    truncated: bool = False
    total_bytes: int = 0
    boundary: dict | None = None


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


def _output_name(prefix: str, suffix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{next(_counter)}{suffix}"


def _spill_path(cwd: Path, prefix: str, suffix: str) -> Path:
    return outputs_dir(cwd) / _output_name(prefix, suffix)


def save_output(text: str, cwd: Path, prefix: str, *, execution_context=None) -> Path:
    if execution_context is not None:
        path, stream = execution_context.open_output(cwd, prefix)
        with stream:
            stream.write(text.encode("utf-8"))
        return path
    path = _spill_path(cwd, prefix, ".txt")
    path.write_text(text, encoding="utf-8")
    return path


def bound(text: str, cwd: Path, prefix: str, keep: str = "head", *, execution_context=None) -> CommandOutput:
    """Bound in-memory text, spilling the full text to disk when it is cut."""
    shown, cut = (truncate_tail if keep == "tail" else truncate_head)(text)
    path = save_output(text, cwd, prefix, execution_context=execution_context) if cut else None
    return CommandOutput(shown, path, cut, len(text.encode("utf-8")))


def budget(text: str, cwd: Path, prefix: str, keep: str = "head", *, t=None, execution_context=None) -> str:
    """Bound `text`; if cut, save the full text and append a pointer to it."""
    t = t or Translator()
    bounded = bound(text, cwd, prefix, keep, execution_context=execution_context)
    if not bounded.truncated:
        return text
    where = t("tools.output.last" if keep == "tail" else "tools.output.first")
    return t(
        "tools.output.budget",
        shown=bounded.text.rstrip(),
        where=where,
        count=len(bounded.text.splitlines()),
        total=len(text.splitlines()),
        path=bounded.path,
    )


def open_output(cwd: Path, prefix: str, *, execution_context=None):
    """Create output through the captured provider; Host needs no isolation imports."""
    if execution_context is not None:
        return execution_context.open_output(cwd, prefix)
    fd, name = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".log", dir=outputs_dir(cwd))
    return Path(name), os.fdopen(fd, "wb", buffering=0)


def read_output(path: Path, *, max_bytes: int = MAX_BYTES, max_lines: int = MAX_LINES,
                execution_context=None) -> CommandOutput:
    from ..execution import HostContext

    access = execution_context or HostContext(path.parent)
    data, total = access.read_output_tail(path, max_bytes)
    start = max(0, total - max_bytes)
    # Don't render the continuation bytes of a UTF-8 character cut by the seek.
    if start:
        while data and data[0] & 0xC0 == 0x80:
            data = data[1:]
    text, cut = truncate_tail(
        data.decode("utf-8", errors="replace"), max_bytes=max_bytes, max_lines=max_lines
    )
    return CommandOutput(text, path, start > 0 or cut, total)


def display_output(output: CommandOutput, *, max_bytes: int = MAX_BYTES, t=None) -> str:
    t = t or Translator()
    text = output.text.rstrip() or t("tools.output.empty")
    if not output.truncated and len(text.encode("utf-8")) <= max_bytes:
        return text
    header = t(
        "tools.output.truncated",
        max_lines=MAX_LINES,
        max_kb=MAX_BYTES // 1024,
        total=output.total_bytes,
        path=output.path,
    )
    # Include the pointer in the budget so the harness won't truncate the tail again.
    available = max_bytes - len(header.encode("utf-8"))
    if available <= 0:
        return header.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    text, _ = truncate_tail(text, max_bytes=available)
    return header + text
