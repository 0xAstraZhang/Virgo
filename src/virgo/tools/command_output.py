"""Command output lives on disk; only a bounded tail is decoded for the conversation."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .truncate import MAX_BYTES, MAX_LINES, outputs_dir, truncate_tail


@dataclass(frozen=True)
class CommandOutput:
    text: str
    path: Path | None = None
    truncated: bool = False
    total_bytes: int = 0


def open_output(cwd: Path, prefix: str):
    fd, name = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".log", dir=outputs_dir(cwd))
    return Path(name), os.fdopen(fd, "wb", buffering=0)


def read_output(path: Path, *, max_bytes: int = MAX_BYTES, max_lines: int = MAX_LINES) -> CommandOutput:
    with path.open("rb") as stream:
        total = stream.seek(0, os.SEEK_END)
        start = max(0, total - max_bytes)
        stream.seek(start)
        data = stream.read(max_bytes)
    # Don't render the continuation bytes of a UTF-8 character cut by the seek.
    if start:
        while data and data[0] & 0xC0 == 0x80:
            data = data[1:]
    text, cut = truncate_tail(data.decode("utf-8", errors="replace"), max_bytes=max_bytes, max_lines=max_lines)
    return CommandOutput(text, path, start > 0 or cut, total)


def display_output(output: CommandOutput, *, max_bytes: int = MAX_BYTES) -> str:
    text = output.text.rstrip() or "(no output)"
    if not output.truncated and len(text.encode("utf-8")) <= max_bytes:
        return text
    header = (f"[Output truncated: showing the tail (limit {MAX_LINES} lines / {MAX_BYTES // 1024}KB; "
              f"{output.total_bytes} bytes total). Full output: {output.path}]\n")
    # Include the pointer in the budget so the harness won't truncate the tail again.
    available = max_bytes - len(header.encode("utf-8"))
    if available <= 0:
        return header.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    text, _ = truncate_tail(text, max_bytes=available)
    return header + text
