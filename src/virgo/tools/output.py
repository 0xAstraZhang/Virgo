"""Bounded tool output backed by private Broker records and opaque output IDs.

Children receive a pipe, never the private spool descriptor or filesystem path.
"""

from __future__ import annotations

import io
import re
from collections import deque
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


MAX_OUTPUT_CONTEXT = 20
MAX_OUTPUT_MATCHES = 500
DEFAULT_OUTPUT_MATCHES = 100
_SEARCH_LINE_CHARS = 500


def _clip_line(line: str) -> str:
    stripped = line.rstrip("\r\n")
    return stripped if len(stripped) <= _SEARCH_LINE_CHARS else stripped[:_SEARCH_LINE_CHARS] + "…"


def _search_output(
    store,
    record: OutputRecord,
    *,
    pattern: str,
    fixed_strings: bool,
    ignore_case: bool,
    context_lines: int,
    max_matches: int,
    start_line: int,
    byte_limit: int,
    ctx,
):
    from ..agent import ToolResult

    flags = re.IGNORECASE if ignore_case else 0
    compiled = re.escape(pattern) if fixed_strings else pattern
    try:
        rx = re.compile(compiled, flags)
    except re.error as error:
        return ToolResult.error(ctx.t("tools.fs.regex", error=error))

    footer_reserve = len(
        _encode(
            ctx.t("tools.read_output.search_more", count=max_matches, line=999_999_999, next_line=1_000_000_000)
        )
    ) + 2
    body_budget = max(1, byte_limit - footer_reserve)

    out: list[str] = []
    out_bytes = 0
    matches = 0
    last_emitted = 0
    before: deque[tuple[int, str]] = deque(maxlen=context_lines)
    after_remaining = 0
    truncated = False
    stop_line = start_line

    def try_emit(lineno: int, sep: str, text: str) -> bool:
        nonlocal out_bytes, last_emitted
        if lineno <= last_emitted:
            return True
        pieces: list[str] = []
        if out and context_lines > 0 and lineno > last_emitted + 1:
            pieces.append("--")
        pieces.append(f"{lineno}{sep}{text}")
        chunk = ("\n" if out else "") + "\n".join(pieces)
        chunk_bytes = len(_encode(chunk))
        if out and out_bytes + chunk_bytes > body_budget:
            return False
        if not out and chunk_bytes > byte_limit:
            clipped = _cut_bytes(pieces[-1], max(1, body_budget), from_end=False)
            pieces[-1] = clipped
            chunk = "\n".join(pieces)
            chunk_bytes = len(_encode(chunk))
        out.extend(pieces)
        out_bytes += chunk_bytes
        last_emitted = lineno
        return True

    with store.open_read(record) as (raw_stream, total_bytes):
        text_stream = io.TextIOWrapper(raw_stream, encoding="utf-8", errors="replace", newline="")
        try:
            for lineno, raw_line in enumerate(text_stream, 1):
                if lineno % 4096 == 0 and ctx.signal.is_set():
                    return ToolResult.error(ctx.t("tools.read_output.cancelled"), details={"cancelled": True})
                if lineno < start_line:
                    continue
                clipped = _clip_line(raw_line)
                if rx.search(raw_line.rstrip("\r\n")):
                    if matches >= max_matches:
                        truncated = True
                        stop_line = lineno - 1
                        break
                    for prev_no, prev_text in before:
                        if not try_emit(prev_no, "-", prev_text):
                            truncated = True
                            stop_line = prev_no - 1
                            break
                    if truncated:
                        break
                    before.clear()
                    if not try_emit(lineno, ":", clipped):
                        truncated = True
                        stop_line = lineno - 1
                        break
                    matches += 1
                    after_remaining = context_lines
                    stop_line = lineno
                elif after_remaining > 0:
                    if not try_emit(lineno, "-", clipped):
                        truncated = True
                        stop_line = lineno - 1
                        break
                    after_remaining -= 1
                    stop_line = lineno
                elif context_lines > 0:
                    before.append((lineno, clipped))
        finally:
            text_stream.detach()

    if not out:
        return ToolResult.text(
            ctx.t("tools.fs.no_matches"),
            details={
                "output_id": record.id,
                "mode": "search",
                "matches": 0,
                "total_bytes": total_bytes,
                "truncated": False,
                "next_line_offset": None,
            },
        )
    if truncated:
        next_line = max(start_line, stop_line + 1)
        out.append(ctx.t("tools.read_output.search_more", count=matches, line=stop_line, next_line=next_line))
    else:
        next_line = None
    return ToolResult.text(
        "\n".join(out),
        details={
            "output_id": record.id,
            "mode": "search",
            "matches": matches,
            "total_bytes": total_bytes,
            "truncated": truncated,
            "next_line_offset": next_line,
        },
    )


def _read_output_lines(
    store,
    record: OutputRecord,
    *,
    start_line: int,
    max_lines: int,
    byte_limit: int,
    ctx,
):
    from ..agent import ToolResult

    footer_reserve = len(
        _encode(ctx.t("tools.read_output.lines_more", start=start_line, end=999_999_999, next_line=1_000_000_000))
    )
    chunk: list[str] = []
    size = 0
    total_lines = 0
    has_more = False
    end_line = start_line - 1

    with store.open_read(record) as (raw_stream, total_bytes):
        text_stream = io.TextIOWrapper(raw_stream, encoding="utf-8", errors="replace", newline="")
        try:
            for lineno, raw_line in enumerate(text_stream, 1):
                if lineno % 4096 == 0 and ctx.signal.is_set():
                    return ToolResult.error(ctx.t("tools.read_output.cancelled"), details={"cancelled": True})
                total_lines = lineno
                if lineno < start_line:
                    continue
                if len(chunk) >= max_lines:
                    has_more = True
                    break
                formatted = f"{lineno}|{raw_line}"
                line_bytes = len(_encode(formatted))
                if size + line_bytes + footer_reserve > byte_limit:
                    if chunk:
                        has_more = True
                        break
                    if line_bytes > byte_limit:
                        limit_label = (
                            f"{byte_limit // 1024}KB"
                            if byte_limit >= 1024 and byte_limit % 1024 == 0
                            else f"{byte_limit} bytes"
                        )
                        return ToolResult.error(
                            ctx.t("tools.read_output.long_line", line=lineno, limit=limit_label)
                        )
                size += line_bytes
                chunk.append(formatted)
                end_line = lineno
        finally:
            text_stream.detach()

    if total_lines == 0:
        return ToolResult.text(
            ctx.t("tools.output.empty"),
            details={
                "output_id": record.id,
                "mode": "lines",
                "lines": [0, 0],
                "next_line_offset": None,
                "total_bytes": total_bytes,
            },
        )
    if start_line > total_lines:
        return ToolResult.error(ctx.t("tools.fs.offset", offset=start_line, total=total_lines))
    text = "".join(chunk)
    if has_more:
        text += ctx.t("tools.read_output.lines_more", start=start_line, end=end_line, next_line=end_line + 1)
    return ToolResult.text(
        text,
        details={
            "output_id": record.id,
            "mode": "lines",
            "lines": [start_line, end_line],
            "next_line_offset": (end_line + 1) if has_more else None,
            "total_bytes": total_bytes,
        },
    )


def _read_output_tail_lines(
    store,
    record: OutputRecord,
    *,
    line_limit: int,
    byte_limit: int,
    ctx,
):
    from ..agent import ToolResult

    ring: deque[tuple[int, str]] = deque(maxlen=line_limit)
    total_lines = 0
    with store.open_read(record) as (raw_stream, total_bytes):
        text_stream = io.TextIOWrapper(raw_stream, encoding="utf-8", errors="replace", newline="")
        try:
            for lineno, raw_line in enumerate(text_stream, 1):
                if lineno % 4096 == 0 and ctx.signal.is_set():
                    return ToolResult.error(ctx.t("tools.read_output.cancelled"), details={"cancelled": True})
                total_lines = lineno
                ring.append((lineno, raw_line))
        finally:
            text_stream.detach()

    if total_lines == 0:
        return ToolResult.text(
            ctx.t("tools.output.empty"),
            details={
                "output_id": record.id,
                "mode": "tail_lines",
                "lines": [0, 0],
                "total_lines": 0,
                "total_bytes": total_bytes,
            },
        )

    footer_reserve = len(
        _encode(ctx.t("tools.read_output.tail_lines", start=total_lines, end=total_lines, total=total_lines))
    )
    selected: list[tuple[int, str]] = []
    size = 0
    for lineno, raw_line in reversed(ring):
        formatted = f"{lineno}|{raw_line}"
        line_bytes = len(_encode(formatted))
        reserve = footer_reserve if lineno > 1 else 0
        if size + line_bytes + reserve > byte_limit and selected:
            break
        if not selected and line_bytes > byte_limit:
            formatted = _cut_bytes(formatted, max(1, byte_limit - reserve), from_end=True)
            line_bytes = len(_encode(formatted))
        selected.append((lineno, formatted))
        size += line_bytes
    selected.reverse()
    first_no = selected[0][0]
    last_no = selected[-1][0]
    text = "".join(item for _, item in selected)
    if first_no > 1:
        text += ctx.t("tools.read_output.tail_lines", start=first_no, end=last_no, total=total_lines)
    return ToolResult.text(
        text,
        details={
            "output_id": record.id,
            "mode": "tail_lines",
            "lines": [first_no, last_no],
            "total_lines": total_lines,
            "total_bytes": total_bytes,
        },
    )


def make_read_output_tool(resolve_context):
    from ..agent import AgentTool, ToolResult
    from ..execution import FileAccessDenied

    def execute(arguments, context):
        access = resolve_context(context)
        try:
            record = access.outputs.lookup(arguments["output_id"])
            raw_limit = arguments.get("limit")
            raw_offset = arguments.get("offset")
            tail = bool(arguments.get("tail"))
            raw_line_offset = arguments.get("line_offset")
            raw_line_limit = arguments.get("line_limit")
            pattern = arguments.get("pattern")
            fixed_strings = bool(arguments.get("fixed_strings"))
            ignore_case = bool(arguments.get("ignore_case"))
            raw_context = arguments.get("context")
            raw_max_matches = arguments.get("max_matches")

            byte_limit = min(MAX_BYTES, max(1, MAX_BYTES if raw_limit is None else int(raw_limit)))

            if pattern is not None:
                if not isinstance(pattern, str) or not pattern:
                    return ToolResult.error(context.t("tools.read_output.empty_pattern"))
                if raw_offset is not None or tail or raw_line_limit is not None:
                    return ToolResult.error(context.t("tools.read_output.conflict_pattern"))
                start_line = 1 if raw_line_offset is None else max(1, int(raw_line_offset))
                ctxn = min(MAX_OUTPUT_CONTEXT, max(0, 0 if raw_context is None else int(raw_context)))
                max_matches = min(
                    MAX_OUTPUT_MATCHES,
                    max(1, DEFAULT_OUTPUT_MATCHES if raw_max_matches is None else int(raw_max_matches)),
                )
                return _search_output(
                    access.outputs,
                    record,
                    pattern=pattern,
                    fixed_strings=fixed_strings,
                    ignore_case=ignore_case,
                    context_lines=ctxn,
                    max_matches=max_matches,
                    start_line=start_line,
                    byte_limit=byte_limit,
                    ctx=context,
                )

            if fixed_strings or ignore_case or (raw_context is not None and int(raw_context) > 0) or raw_max_matches is not None:
                return ToolResult.error(context.t("tools.read_output.pattern_required"))
            if raw_offset is not None and (tail or raw_line_offset is not None or raw_line_limit is not None):
                return ToolResult.error(context.t("tools.read_output.conflict_offset"))
            if tail and raw_line_offset is not None:
                return ToolResult.error(context.t("tools.read_output.conflict_tail"))

            if tail and raw_line_limit is not None:
                line_limit = min(MAX_LINES, max(1, int(raw_line_limit)))
                return _read_output_tail_lines(
                    access.outputs, record, line_limit=line_limit, byte_limit=byte_limit, ctx=context
                )

            if raw_line_offset is not None or raw_line_limit is not None:
                start_line = 1 if raw_line_offset is None else max(1, int(raw_line_offset))
                line_limit = min(MAX_LINES, max(1, MAX_LINES if raw_line_limit is None else int(raw_line_limit)))
                return _read_output_lines(
                    access.outputs,
                    record,
                    start_line=start_line,
                    max_lines=line_limit,
                    byte_limit=byte_limit,
                    ctx=context,
                )

            if tail:
                data, total, start = access.outputs.read(record, limit=byte_limit, tail=True, align_utf8=True)
                return ToolResult.text(
                    data.decode("utf-8", errors="replace"),
                    details={
                        "output_id": record.id,
                        "offset": start,
                        "next_offset": None,
                        "total_bytes": total,
                        "tail": True,
                    },
                )

            offset = 0 if raw_offset is None else int(raw_offset)
            data, total, start = access.outputs.read(record, offset=offset, limit=byte_limit, align_utf8=True)
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
        description=(
            "Inspect a private command/tool output by output_id. Supports pattern search (pattern, "
            "fixed_strings, ignore_case, context, max_matches), tail reading (tail=true, optionally with "
            "line_limit), 1-based line paging (line_offset, line_limit), or 0-based byte paging (offset, limit)."
        ),
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
                "tail": {
                    "type": "boolean",
                    "description": "Read from the end of the output (last limit bytes, or last line_limit lines).",
                },
                "line_offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based line number to start reading or searching from.",
                },
                "line_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LINES,
                    "description": "Maximum number of lines to return.",
                },
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Search output lines with a Python regular expression (or literal string with fixed_strings=true).",
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": "Treat pattern as a literal string instead of a regular expression.",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Perform case-insensitive pattern matching.",
                },
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_OUTPUT_CONTEXT,
                    "description": "Lines of context before and after each match.",
                },
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_OUTPUT_MATCHES,
                    "description": "Maximum number of matches to return when searching with pattern (default 100).",
                },
            },
            "required": ["output_id"],
            "additionalProperties": False,
        },
        execute=execute,
        i18n_key="tools.read_output",
    )
