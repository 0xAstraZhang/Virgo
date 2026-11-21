"""File tools: read, write, edit, glob, grep (pure Python, enforced by PermissionPolicy)."""

from __future__ import annotations

import base64
import difflib
import fnmatch
import re
import threading
from contextlib import ExitStack
from dataclasses import replace
from functools import cache
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult, image_result
from ..execution import FileAccessDenied, HostContext
from ..file_access import MAX_WALK_FILES, resolve_read_path, resolve_write_path
from .output import MAX_BYTES, MAX_LINES

IGNORE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    "dist",
    "build",
}
BOM = "\ufeff"
MAX_EDIT_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def file_lock(path: Path) -> threading.Lock:
    """Serialize mutations of the same file when tools run in parallel."""
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def _display(p: Path, cwd: Path) -> str:
    try:
        return str(p.relative_to(Path(cwd).resolve()))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _read(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    access = access or HostContext(ctx.cwd)
    try:
        p = resolve_read_path(args["path"], ctx.cwd)
    except ValueError as error:
        return ToolResult.error(str(error))
    access.check_read(p)
    if not p.exists():
        return ToolResult.error(ctx.t("tools.fs.file_missing", path=args["path"]))
    if p.is_dir():
        return ToolResult.error(ctx.t("tools.fs.is_directory", path=args["path"]))
    try:
        if p.suffix.lower() in IMAGE_TYPES:
            data = access.read_bytes(p)
            if len(data) > 5 * 1024 * 1024:
                return ToolResult.error(ctx.t("tools.fs.large_image"))
            return image_result(
                base64.b64encode(data).decode(),
                IMAGE_TYPES[p.suffix.lower()],
                ctx.t("tools.fs.image", name=p.name),
            )
        raw = access.read_bytes(p)
    except FileNotFoundError:
        return ToolResult.error(ctx.t("tools.fs.file_missing", path=args["path"]))
    except IsADirectoryError:
        return ToolResult.error(ctx.t("tools.fs.is_directory", path=args["path"]))
    if b"\x00" in raw[:8192]:
        return ToolResult.error(ctx.t("tools.fs.binary", path=args["path"]))
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    total = len(lines)
    offset = max(1, int(args.get("offset") or 1))
    limit = int(args.get("limit") or MAX_LINES)
    if offset > max(total, 1):
        return ToolResult.error(ctx.t("tools.fs.offset", offset=offset, total=total))
    chunk, size = [], 0
    for line in lines[offset - 1 : offset - 1 + min(limit, MAX_LINES)]:
        size += len(line.encode("utf-8"))
        if size > MAX_BYTES and chunk:
            break
        chunk.append(line)
    if not chunk and lines:
        n = offset
        return ToolResult.error(
            ctx.t("tools.fs.long_line", line=n, max_kb=MAX_BYTES // 1024, path=args["path"])
        )
    text = "".join(chunk)
    end = offset + len(chunk) - 1
    if end < total:
        text += ctx.t("tools.fs.page", offset=offset, end=end, total=total, next_offset=end + 1)
    return ToolResult.text(
        text or ctx.t("tools.fs.empty"), details={"path": str(p), "lines": [offset, end], "total": total}
    )


READ = AgentTool(
    name="read",
    label="Read",
    description=(
        f"Read a text file (or an image). Returns at most {MAX_LINES} lines / {MAX_BYTES // 1024}KB per call; "
        "use offset (1-based line number) and limit to page through larger files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, relative to the working directory or absolute",
            },
            "offset": {"type": "integer", "minimum": 1, "description": "1-based line to start from"},
            "limit": {"type": "integer", "minimum": 1, "description": "Maximum number of lines"},
        },
        "required": ["path"],
    },
    execute=_read,
    side_effect="read",
    execution_scope="file",
    i18n_key="tools.read",
)

# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _write(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    access = access or HostContext(ctx.cwd)
    try:
        p = resolve_write_path(args["path"], ctx.cwd)
    except ValueError as error:
        return ToolResult.error(str(error))
    data = args["content"].encode("utf-8", errors="replace")
    with file_lock(p):
        try:
            with access.open_mutation(p, create=True) as (stream, existed, commit):
                stream.write(data)
                stream.truncate()
                commit()
        except IsADirectoryError:
            return ToolResult.error(ctx.t("tools.fs.is_directory", path=args["path"]))
    n = len(data)
    verb = ctx.t("tools.fs.overwrite") if existed else ctx.t("tools.fs.create")
    return ToolResult.text(
        ctx.t("tools.fs.written", verb=verb, path=_display(p, ctx.cwd), size=n),
        details={"path": str(p), "created": not existed},
    )


WRITE = AgentTool(
    name="write",
    label="Write",
    description="Create or overwrite a file with the given content. Parent directories are created.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    execute=_write,
    execution_scope="file",
    i18n_key="tools.write",
)

# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2009": " ",
        "\u202f": " ",
        "\u3000": " ",
    }
)


def _find_all(hay: str, needle: str) -> list[int]:
    out, i = [], hay.find(needle)
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def _locate(content: str, old: str) -> tuple[list[tuple[int, int]], str]:
    """Find spans of `old` in `content`. Exact first, then tolerant matching."""
    spans = [(i, i + len(old)) for i in _find_all(content, old)]
    if spans:
        return spans, "exact"
    # 1:1 character normalization keeps offsets aligned
    nc, no = content.translate(_CHAR_MAP), old.translate(_CHAR_MAP)
    spans = [(i, i + len(no)) for i in _find_all(nc, no)]
    if spans:
        return spans, "normalized"
    # line-based match ignoring trailing whitespace
    c_lines = content.split("\n")
    o_lines = old.translate(_CHAR_MAP).split("\n")
    ends_with_newline = bool(o_lines) and o_lines[-1] == ""
    if ends_with_newline:
        o_lines = o_lines[:-1]
    key = [line.rstrip() for line in o_lines]
    if not key:
        return [], ""
    starts = [0]
    for line in c_lines:
        starts.append(starts[-1] + len(line) + 1)
    norm = [line.translate(_CHAR_MAP).rstrip() for line in c_lines]
    for i in range(len(c_lines) - len(key) + 1):
        if norm[i : i + len(key)] == key:
            end = starts[i + len(key)] - 1  # the newline ending the last matched line, or end of file
            if ends_with_newline and end < len(content):
                end += 1  # old_text covered that newline too; new_text supplies its own
            spans.append((starts[i], end))
    return spans, "whitespace" if spans else ""


def _prepare_edit(args: dict) -> dict:
    import json

    a = dict(args)
    if isinstance(a.get("edits"), str):
        try:
            a["edits"] = json.loads(a["edits"])
        except json.JSONDecodeError:
            pass
    old = a.pop("old_text", a.pop("oldText", None))
    new = a.pop("new_text", a.pop("newText", None))
    if old is not None and "edits" not in a:
        a["edits"] = [{"old_text": old, "new_text": new or ""}]
    if isinstance(a.get("edits"), list):
        a["edits"] = [
            {
                "old_text": e.get("old_text", e.get("oldText")),
                "new_text": e.get("new_text", e.get("newText", "")),
            }
            if isinstance(e, dict)
            else e
            for e in a["edits"]
        ]
    return a


def _edit(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    access = access or HostContext(ctx.cwd)
    try:
        p = resolve_write_path(args["path"], ctx.cwd)
    except ValueError as error:
        return ToolResult.error(str(error))
    access.check_write(p)
    with file_lock(p), ExitStack() as stack:
        try:
            stream, _, commit = stack.enter_context(
                access.open_mutation(p, create=False, max_read_bytes=MAX_EDIT_BYTES)
            )
        except FileNotFoundError:
            return ToolResult.error(ctx.t("tools.fs.file_missing", path=args["path"]))
        except IsADirectoryError:
            return ToolResult.error(ctx.t("tools.fs.is_directory", path=args["path"]))
        except ValueError as error:
            return ToolResult.error(str(error))
        raw = stream.read().decode("utf-8")
        # No newline translation: CRLF is preserved explicitly.
        bom = raw.startswith(BOM)
        if bom:
            raw = raw[1:]
        crlf = "\r\n" in raw
        content = raw.replace("\r\n", "\n")
        spans: list[tuple[int, int, str]] = []
        for n, e in enumerate(args["edits"], 1):
            old = (e.get("old_text") or "").replace("\r\n", "\n")
            new = (e.get("new_text") or "").replace("\r\n", "\n")
            if not old:
                return ToolResult.error(ctx.t("tools.fs.edit_empty", index=n))
            found, _how = _locate(content, old)
            if not found:
                return ToolResult.error(ctx.t("tools.fs.edit_missing", index=n, path=args["path"]))
            if len(found) > 1:
                return ToolResult.error(
                    ctx.t("tools.fs.edit_ambiguous", index=n, count=len(found), path=args["path"])
                )
            spans.append((found[0][0], found[0][1], new))
        spans.sort()
        for (_, e1, _), (s2, _, _) in zip(spans, spans[1:], strict=False):
            if s2 < e1:
                return ToolResult.error(ctx.t("tools.fs.edit_overlap"))
        updated = content
        for s, e, new in reversed(spans):
            updated = updated[:s] + new + updated[e:]
        if updated == content:
            return ToolResult.error(ctx.t("tools.fs.edit_unchanged"))
        final = updated.replace("\n", "\r\n") if crlf else updated
        final = (BOM if bom else "") + final
        stream.seek(0)
        stream.write(final.encode("utf-8", errors="replace"))
        stream.truncate()
        commit()
    diff = "".join(
        difflib.unified_diff(
            content.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            "a/" + p.name,
            "b/" + p.name,
            n=2,
        )
    )
    return ToolResult.text(
        ctx.t(
            "tools.fs.edited",
            path=_display(p, ctx.cwd),
            count=len(spans),
            replacements=ctx.t("tools.fs.replacements" if len(spans) > 1 else "tools.fs.replacement"),
        ),
        details={"path": str(p), "diff": diff[:20000]},
    )


EDIT = AgentTool(
    name="edit",
    label="Edit",
    description=(
        "Replace exact text in a file. Each edit's old_text must match exactly one place in the current file "
        "(include enough context to be unique); edits must not overlap. Several edits to one file can be sent "
        "in a single call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}},
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["path", "edits"],
    },
    execute=_edit,
    execution_scope="file",
    prepare_arguments=_prepare_edit,
    i18n_key="tools.edit",
)


def make_file_tool(tool: AgentTool, execution) -> AgentTool:
    """Bind file operations to an immutable caller-owned execution context."""
    def execute(args, ctx):
        access = execution(ctx)
        try:
            result = tool.execute(args, ctx, access=access)
        except FileAccessDenied as error:
            facts = {**access.describe(), "kind": "resource_denied", "dimension": error.dimension,
                     "target": error.target, "payload_started": False,
                     "side_effects": "unknown" if tool.name in {"write", "edit"} else "none"}
            return ToolResult.error(str(error), details={"status": "resource_denied", "boundary": facts})
        result.details = {**(result.details or {}), "boundary": {
            **access.describe(), "payload_started": True,
            "side_effects": "possible" if tool.name in {"write", "edit"} else "none",
        }}
        return result

    return replace(tool, execute=execute, execution_scope="file")

# ---------------------------------------------------------------------------
# glob / grep
# ---------------------------------------------------------------------------


def _collapse_glob(pattern: str) -> tuple[str, ...]:
    raw = pattern.split("/")
    if len(raw) == 1:
        raw = ["**", pattern]
    collapsed: list[str] = []
    for seg in raw:
        if seg == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(seg)
    return tuple(collapsed)


def glob_match(path: str, pattern: str) -> bool:
    """Match a ``/``-separated path against a glob in O(N*M) time.

    ``*``, ``?`` and ``[...]`` stay within one path segment; consecutive ``**``
    segments are collapsed to prevent exponential backtracking. A pattern without
    ``/`` matches the file name at any depth.
    """
    parts = tuple(path.split("/"))
    segments = _collapse_glob(pattern)

    @cache
    def match(pi: int, si: int) -> bool:
        if si == len(segments):
            return pi == len(parts)
        head = segments[si]
        if head == "**":
            if si + 1 == len(segments):
                return pi < len(parts)
            return match(pi, si + 1) or (pi < len(parts) and match(pi + 1, si))
        return pi < len(parts) and fnmatch.fnmatchcase(parts[pi], head) and match(pi + 1, si + 1)

    return match(0, 0)


def _glob(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    access = access or HostContext(ctx.cwd)
    try:
        base = resolve_read_path(args.get("path") or ".", ctx.cwd)
    except ValueError as error:
        return ToolResult.error(str(error))
    access.check_read(base)
    if not base.is_dir():
        return ToolResult.error(ctx.t("tools.fs.not_directory", path=args.get("path")))
    pattern = args["pattern"]
    hits = []
    entries = access.walk_files(base, IGNORE_DIRS, max_files=MAX_WALK_FILES)
    modified = {}
    for f, info in entries:
        if glob_match(f.relative_to(base).as_posix(), pattern):
            hits.append(f)
            modified[f] = info.st_mtime if info is not None else 0
    hits.sort(key=lambda f: modified[f], reverse=True)
    limit = 200
    lines = [_display(f, ctx.cwd) for f in hits[:limit]]
    if not lines:
        return ToolResult.text(ctx.t("tools.fs.no_matches"))
    if len(hits) > limit:
        lines.append(ctx.t("tools.fs.more_matches", count=len(hits) - limit))
    return ToolResult.text("\n".join(lines), details={"count": len(hits)})


GLOB = AgentTool(
    name="glob",
    label="Glob",
    description="Find files by glob pattern (e.g. '**/*.py', 'src/**/test_*.ts'), newest first. "
    "Skips .git, node_modules, virtualenvs and caches.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search"},
        },
        "required": ["pattern"],
    },
    execute=_glob,
    side_effect="read",
    execution_scope="file",
    i18n_key="tools.glob",
)


def _grep(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    access = access or HostContext(ctx.cwd)
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        rx = re.compile(args["pattern"], flags)
    except re.error as e:
        return ToolResult.error(ctx.t("tools.fs.regex", error=e))
    try:
        base = resolve_read_path(args.get("path") or ".", ctx.cwd)
    except ValueError as error:
        return ToolResult.error(str(error))
    access.check_read(base)
    base_is_dir = base.is_dir()
    files = [base] if base.is_file() else (
        path for path, _info in access.walk_files(base, IGNORE_DIRS, max_files=MAX_WALK_FILES)
    )
    gl = args.get("glob")
    ctxn = int(args.get("context") or 0)
    limit = int(args.get("limit") or 100)
    out: list[str] = []
    matches = 0
    for f in files:
        if gl:
            rel = f.relative_to(base).as_posix() if base_is_dir else f.name
            if not glob_match(rel, gl):
                continue
        try:
            raw = access.read_bytes(f, max_bytes=5 * 1024 * 1024)
            if raw is None or len(raw) > 5 * 1024 * 1024:
                continue
        except FileAccessDenied:
            raise
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        shown: set[int] = set()
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            matches += 1
            if matches > limit:
                break
            for j in range(max(0, i - ctxn), min(len(lines), i + ctxn + 1)):
                if j in shown:
                    continue
                shown.add(j)
                sep = ":" if j == i else "-"
                text = lines[j] if len(lines[j]) <= 500 else lines[j][:500] + "…"
                out.append(f"{_display(f, ctx.cwd)}{sep}{j + 1}{sep}{text}")
        if matches > limit:
            break
    if not out:
        return ToolResult.text(ctx.t("tools.fs.no_matches"))
    if matches > limit:
        out.append(ctx.t("tools.fs.match_limit", limit=limit))
    return ToolResult.text("\n".join(out), details={"matches": min(matches, limit)})


GREP = AgentTool(
    name="grep",
    label="Grep",
    description="Search file contents with a Python regular expression. Output lines are 'path:line:text'.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
            "ignore_case": {"type": "boolean"},
            "context": {"type": "integer", "minimum": 0, "description": "Lines of context around matches"},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["pattern"],
    },
    execute=_grep,
    side_effect="read",
    execution_scope="file",
    i18n_key="tools.grep",
)
