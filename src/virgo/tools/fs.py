"""File tools: read, write, edit, glob, grep (pure Python, no external binaries)."""

from __future__ import annotations

import base64
import difflib
import fnmatch
import os
import re
import threading
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult, image_result
from .truncate import MAX_BYTES, MAX_LINES

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
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def resolve(path: str, cwd: Path) -> Path:
    p = Path(os.path.expanduser(path))
    return (p if p.is_absolute() else Path(cwd) / p).resolve()


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


def _read(args: dict, ctx: ToolContext) -> ToolResult:
    p = resolve(args["path"], ctx.cwd)
    if not p.exists():
        return ToolResult.error(f"File not found: {args['path']}")
    if p.is_dir():
        return ToolResult.error(f"{args['path']} is a directory. Use glob to list files.")
    if p.suffix.lower() in IMAGE_TYPES:
        data = p.read_bytes()
        if len(data) > 5 * 1024 * 1024:
            return ToolResult.error("Image is larger than 5MB.")
        return image_result(
            base64.b64encode(data).decode(), IMAGE_TYPES[p.suffix.lower()], f"Image: {p.name}"
        )
    raw = p.read_bytes()
    if b"\x00" in raw[:8192]:
        return ToolResult.error(f"{args['path']} looks like a binary file.")
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    total = len(lines)
    offset = max(1, int(args.get("offset") or 1))
    limit = int(args.get("limit") or MAX_LINES)
    if offset > max(total, 1):
        return ToolResult.error(f"offset {offset} is past the end of the file ({total} lines).")
    chunk, size = [], 0
    for line in lines[offset - 1 : offset - 1 + min(limit, MAX_LINES)]:
        size += len(line.encode("utf-8"))
        if size > MAX_BYTES and chunk:
            break
        chunk.append(line)
    if not chunk and lines:
        n = offset
        return ToolResult.error(
            f"Line {n} exceeds {MAX_BYTES // 1024}KB. Use bash: sed -n '{n}p' {args['path']} | head -c 20000"
        )
    text = "".join(chunk)
    end = offset + len(chunk) - 1
    if end < total:
        text += f"\n[Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.]"
    return ToolResult.text(
        text or "(empty file)", details={"path": str(p), "lines": [offset, end], "total": total}
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
    snippet="read files (text or images), paging with offset/limit",
    read_only=True,
)

# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _write(args: dict, ctx: ToolContext) -> ToolResult:
    p = resolve(args["path"], ctx.cwd)
    with file_lock(p):
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
    n = len(args["content"].encode("utf-8"))
    verb = "Overwrote" if existed else "Created"
    return ToolResult.text(
        f"{verb} {_display(p, ctx.cwd)} ({n} bytes).", details={"path": str(p), "created": not existed}
    )


WRITE = AgentTool(
    name="write",
    label="Write",
    description="Create or overwrite a file with the given content. Parent directories are created. "
    "Prefer edit for changing part of an existing file.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    execute=_write,
    snippet="create or overwrite files",
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
    if o_lines and o_lines[-1] == "":
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
            end = starts[i + len(key)] - 1
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


def _edit(args: dict, ctx: ToolContext) -> ToolResult:
    p = resolve(args["path"], ctx.cwd)
    if not p.is_file():
        return ToolResult.error(f"File not found: {args['path']}")
    with file_lock(p):
        raw = p.read_bytes().decode("utf-8")  # no newline translation: CRLF is preserved explicitly
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
                return ToolResult.error(f"edit {n}: old_text must not be empty.")
            found, _how = _locate(content, old)
            if not found:
                return ToolResult.error(
                    f"edit {n}: old_text not found in {args['path']}. Read the file again and copy the exact text."
                )
            if len(found) > 1:
                return ToolResult.error(
                    f"edit {n}: old_text matches {len(found)} places in {args['path']}. "
                    "Include more surrounding lines so it is unique."
                )
            spans.append((found[0][0], found[0][1], new))
        spans.sort()
        for (_, e1, _), (s2, _, _) in zip(spans, spans[1:], strict=False):
            if s2 < e1:
                return ToolResult.error("Edits overlap; merge them into a single edit.")
        updated = content
        for s, e, new in reversed(spans):
            updated = updated[:s] + new + updated[e:]
        if updated == content:
            return ToolResult.error("No changes: new_text is identical to old_text.")
        final = updated.replace("\n", "\r\n") if crlf else updated
        p.write_text((BOM if bom else "") + final, encoding="utf-8", newline="")
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
        f"Edited {_display(p, ctx.cwd)} ({len(spans)} replacement{'s' if len(spans) > 1 else ''}).",
        details={"path": str(p), "diff": diff[:20000]},
    )


EDIT = AgentTool(
    name="edit",
    label="Edit",
    description=(
        "Replace exact text in a file. Each edit's old_text must match exactly one place in the current file "
        "(include enough context to be unique); edits must not overlap. Several edits to one file can be sent "
        "in a single call. Read the file first."
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
    prepare_arguments=_prepare_edit,
    snippet="precise find-and-replace edits (old_text must be unique)",
)

# ---------------------------------------------------------------------------
# glob / grep
# ---------------------------------------------------------------------------


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for f in filenames:
            yield Path(dirpath) / f


def glob_match(path: str, pattern: str) -> bool:
    """Match a ``/``-separated path against a glob.

    ``*``, ``?`` and ``[...]`` stay within one path segment; a ``**`` segment spans any number of
    directories, including none. A pattern without ``/`` matches the file name at any depth.
    """
    segments = pattern.split("/")
    if len(segments) == 1:
        segments = ["**", pattern]
    return _match_segments(path.split("/"), segments)


def _match_segments(parts: list[str], segments: list[str]) -> bool:
    if not segments:
        return not parts
    head, rest = segments[0], segments[1:]
    if head == "**":
        if not rest:
            return bool(parts)
        return any(_match_segments(parts[i:], rest) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match_segments(parts[1:], rest)


def _glob(args: dict, ctx: ToolContext) -> ToolResult:
    base = resolve(args.get("path") or ".", ctx.cwd)
    if not base.is_dir():
        return ToolResult.error(f"Not a directory: {args.get('path')}")
    pattern = args["pattern"]
    hits = []
    for f in _walk(base):
        if glob_match(f.relative_to(base).as_posix(), pattern):
            hits.append(f)
    hits.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
    limit = 200
    lines = [_display(f, ctx.cwd) for f in hits[:limit]]
    if not lines:
        return ToolResult.text("(no matches)")
    if len(hits) > limit:
        lines.append(f"... {len(hits) - limit} more; narrow the pattern")
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
    snippet="find files by glob pattern",
    read_only=True,
)


def _grep(args: dict, ctx: ToolContext) -> ToolResult:
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        rx = re.compile(args["pattern"], flags)
    except re.error as e:
        return ToolResult.error(f"Invalid regex: {e}")
    base = resolve(args.get("path") or ".", ctx.cwd)
    files = [base] if base.is_file() else _walk(base)
    gl = args.get("glob")
    ctxn = int(args.get("context") or 0)
    limit = int(args.get("limit") or 100)
    out: list[str] = []
    matches = 0
    for f in files:
        if gl and not glob_match(Path(_display(f, ctx.cwd)).as_posix(), gl):
            continue
        try:
            if f.stat().st_size > 5 * 1024 * 1024:
                continue
            raw = f.read_bytes()
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
        return ToolResult.text("(no matches)")
    if matches > limit:
        out.append(f"... stopped after {limit} matches; refine the pattern or raise limit")
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
    snippet="search file contents by regex",
    read_only=True,
)
