"""Command output lives on disk; only a bounded tail is decoded for the conversation."""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..i18n import Translator
from .truncate import MAX_BYTES, MAX_LINES, outputs_dir, truncate_tail


@dataclass(frozen=True)
class CommandOutput:
    text: str
    path: Path | None = None
    truncated: bool = False
    total_bytes: int = 0


def open_output(cwd: Path, prefix: str, *, sandbox_policy=None):
    if sandbox_policy is not None:
        from .sandbox import SandboxUnavailable

        sandbox_policy.validate(cwd)
        if os.name == "nt":
            from .windows_sandbox_files import open_output as windows_output

            try:
                return windows_output(sandbox_policy, prefix)
            except OSError as error:
                raise SandboxUnavailable(f"Could not open originating Windows sandbox output: {error}") from error
        # Logs belong to the originating workspace. Walk by directory descriptor
        # so a metadata symlink cannot redirect broker writes between check/open.
        directory = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(sandbox_policy.workspace, flags)
            for part in (".virgo", "outputs"):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            name = f"{prefix}-{uuid.uuid4().hex}.log"
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            path = sandbox_policy.workspace / ".virgo" / "outputs" / name
            return path, os.fdopen(fd, "wb", buffering=0)
        except OSError as error:
            raise SandboxUnavailable(f"Could not open originating sandbox output: {error}") from error
        finally:
            if directory is not None:
                os.close(directory)
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
