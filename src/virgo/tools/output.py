"""Bounded tool output backed by private Broker records and opaque output IDs.

Children receive a pipe, never the private spool descriptor or filesystem path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..i18n import Translator
from ..output_store import OutputRecord, private_output_store

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


@dataclass(frozen=True)
class CommandOutput:
    text: str
    path: OutputRecord | Path | None = None
    truncated: bool = False
    total_bytes: int = 0
    boundary: dict | None = None


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="replace")


def _cut_bytes(text: str, max_bytes: int, from_end: bool) -> str:
    data = _encode(text)
    if len(data) <= max_bytes:
        return text
    data = data[-max_bytes:] if from_end else data[:max_bytes]
    return data.decode("utf-8", errors="ignore")


def truncate_head(text: str, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    cut = len(lines) > max_lines
    out = "".join(lines[:max_lines])
    if len(_encode(out)) > max_bytes:
        out = _cut_bytes(out, max_bytes, from_end=False)
        out = out[: out.rfind("\n") + 1] or out
        cut = True
    return out, cut


def truncate_tail(text: str, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    cut = len(lines) > max_lines
    out = "".join(lines[-max_lines:]) if cut else text
    if len(_encode(out)) > max_bytes:
        out = _cut_bytes(out, max_bytes, from_end=True)
        nl = out.find("\n")
        out = out[nl + 1 :] if 0 <= nl < len(out) - 1 else out
        cut = True
    return out, cut


def save_output(text: str, cwd: Path, prefix: str, *, execution_context=None) -> OutputRecord:
    record, stream = open_output(cwd, prefix, execution_context=execution_context)
    try:
        with stream:
            stream.write(_encode(text))
    except BaseException:
        record.unlink(missing_ok=True)
        raise
    return record


def bound(text: str, cwd: Path, prefix: str, keep: str = "head", *, max_bytes: int = MAX_BYTES,
          execution_context=None) -> CommandOutput:
    """Bound in-memory text, spilling the full text to private storage when it is cut."""
    shown, cut = (truncate_tail if keep == "tail" else truncate_head)(text, max_bytes=max_bytes)
    record = save_output(text, cwd, prefix, execution_context=execution_context) if cut else None
    return CommandOutput(shown, record, cut, len(_encode(text)))


def exceeds_budget(text: str) -> bool:
    return len(text) > MAX_BYTES or len(_encode(text)) > MAX_BYTES or len(text.splitlines()) > MAX_LINES


def budget(text: str, cwd: Path, prefix: str, keep: str = "head", *, t=None, execution_context=None) -> str:
    """Fit both the preview and its private output ID within the byte and line limits."""
    if not exceeds_budget(text):
        return text
    record = save_output(text, cwd, prefix, execution_context=execution_context)
    t = t or Translator()
    total = len(text.splitlines())
    where = t("tools.output.last" if keep == "tail" else "tools.output.first")

    def render(shown: str, count: int) -> str:
        return t("tools.output.budget", shown=shown, where=where, count=count, total=total, path=record)

    # Reserve space for the largest possible count, the separators and the actual ID.
    notice = render("", min(total, MAX_LINES))
    truncate = truncate_tail if keep == "tail" else truncate_head
    shown, _ = truncate(text, max_lines=MAX_LINES - len(notice.splitlines()),
                        max_bytes=MAX_BYTES - len(_encode(notice)))
    return render(shown, len(shown.splitlines()))


def open_output(cwd: Path, prefix: str, *, execution_context=None):
    if execution_context is not None:
        return execution_context.open_output(cwd, prefix)
    return private_output_store().create(prefix)


def read_output(record: OutputRecord, *, max_bytes: int = MAX_BYTES, max_lines: int = MAX_LINES,
                execution_context=None) -> CommandOutput:
    store = execution_context.outputs if execution_context is not None else record.store
    data, total, start = store.read(record, limit=max_bytes, tail=True)
    # Don't render the continuation bytes of a UTF-8 character cut by the seek.
    if start:
        while data and data[0] & 0xC0 == 0x80:
            data = data[1:]
    text, cut = truncate_tail(
        data.decode("utf-8", errors="replace"), max_bytes=max_bytes, max_lines=max_lines
    )
    return CommandOutput(text, record, start > 0 or cut, total)


def display_output(output: CommandOutput, *, max_bytes: int = MAX_BYTES, reserved_bytes: int = 0, t=None) -> str:
    """Render within the total preview budget, allowing for caller-supplied text."""
    t = t or Translator()
    available = max(0, max_bytes - reserved_bytes)
    text = output.text.rstrip() or t("tools.output.empty")
    if not output.truncated and len(_encode(text)) <= available:
        return text
    header = t(
        "tools.output.truncated",
        max_lines=MAX_LINES,
        max_kb=str(max_bytes / 1024).removesuffix(".0"),
        total=output.total_bytes,
        path=output.path,
    )
    # Include the pointer in the budget so the harness won't truncate the tail again.
    body_bytes = available - len(_encode(header))
    if body_bytes <= 0:
        return _encode(header)[:available].decode("utf-8", errors="ignore")
    text, _ = truncate_tail(text, max_bytes=body_bytes)
    return header + text


def make_read_output_tool(resolve_context):
    from ..agent import AgentTool, ToolResult
    from ..execution import FileAccessDenied

    def execute(arguments, context):
        access = resolve_context(context)
        try:
            record = access.outputs.lookup(arguments["output_id"])
            raw_limit = arguments.get("limit")
            raw_offset = arguments.get("offset")
            limit = min(MAX_BYTES, max(1, MAX_BYTES if raw_limit is None else int(raw_limit)))
            offset = 0 if raw_offset is None else int(raw_offset)
            data, total, start = access.outputs.read(record, offset=offset, limit=limit, align_utf8=True)
        except (FileAccessDenied, OSError, ValueError, TypeError) as error:
            return ToolResult.error(str(error))
        end = start + len(data)
        return ToolResult.text(data.decode("utf-8", errors="replace"), details={
            "output_id": record.id, "offset": start, "next_offset": end if end < total else None,
            "total_bytes": total,
        })

    return AgentTool(
        name="read_output",
        label="Read output",
        execution_scope="broker",
        side_effect="read",
        description="Read a private command/tool output using its output_id. offset and limit are byte counts.",
        parameters={
            "type": "object",
            "properties": {
                "output_id": {
                    "type": "string",
                    "description": "Opaque output_id from a truncated command or tool result.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0-based byte offset to start reading from.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_BYTES,
                    "description": "Maximum number of bytes to read.",
                },
            },
            "required": ["output_id"],
            "additionalProperties": False,
        },
        execute=execute,
        i18n_key="tools.read_output",
    )
