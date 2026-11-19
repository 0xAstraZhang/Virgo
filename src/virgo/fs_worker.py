"""Bounded filesystem operations executed only by the sandbox subprocess.

This module and its Windows companion are copied to a trusted runtime snapshot
before model execution. Keep them independent of Virgo imports and installed
packages: ``python -I -S snapshot.py --stdio`` is the entry point.
The operating-system boundary supplies authority; these routines supply safe
file mechanics and the existing file-tool behavior, never a second grant policy.
"""
from __future__ import annotations

import base64
import difflib
import errno
import fnmatch
import importlib.util
import io
import json
import os
import re
import stat
import struct
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_WIRE_BYTES = 16 * 1024 * 1024
MAX_BYTES = 50 * 1024
MAX_LINES = 2000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_WALK_FILES = 10000
IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".idea", "dist", "build",
}
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
BOM = "\ufeff"
_CHAR_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2013": "-",
    "\u2014": "-", "\u2212": "-", "\u00a0": " ", "\u2009": " ",
    "\u202f": " ", "\u3000": " ",
})


def _windows():
    # -I excludes both cwd and the script directory from import search. Load
    # only the exact companion next to this already trusted worker snapshot.
    name = "_virgo_fs_windows"
    if name not in sys.modules:
        source = Path(__file__).with_name("fs_windows.py")
        spec = importlib.util.spec_from_file_location(name, source)
        if spec is None or spec.loader is None:
            raise RuntimeError("Trusted Windows filesystem helper is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def message(key, **values):
    return {"key": key, "values": values}


def result(*segments, details=None, ok=True, **extra):
    return {"version": 1, "ok": ok, "segments": list(segments), "details": details or {}, **extra}


def failure(key, **values):
    return result(message(key, **values), ok=False)


def _positive(value, default, *, zero=False):
    number = default if value is None else int(value)
    if number < (0 if zero else 1):
        raise ValueError("Numeric file arguments are outside their supported range")
    return number


def resolve(path, cwd, home, *, follow_symlinks=True):
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("File path must be a nonempty string without NUL")
    if path == "~" or path.startswith(("~/", "~\\")):
        path = str(Path(home) / path[2:]) if len(path) > 1 else home
    elif path.startswith("~"):
        path = os.path.expanduser(path)
    target = Path(path)
    target = target if target.is_absolute() else Path(cwd) / target
    if os.name == "nt":
        # LPAC does not grant the DOS-device queries used by strict realpath.
        # Retained native opens below reject filesystem reparses themselves.
        return Path(_windows().normalize_path(target))
    if follow_symlinks:
        target = target.resolve()
    else:
        target = Path(os.path.abspath(target))
    return target


def _display(path, cwd):
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


@contextmanager
def _directory(path, *, create=False):
    """Retain the real parent object, rejecting links in every component."""
    if os.name == "nt":
        win = _windows()
        pending, current = [], path
        while True:
            try:
                parent = win.open_path(current, 0x80, directory=True, share_delete=False)
                break
            except FileNotFoundError:
                if not create or current == current.parent:
                    raise
                pending.append(current.name)
                current = current.parent
        try:
            for part in reversed(pending):
                child = win.open_child(parent, part, 0x80, directory=True,
                                       disposition=win.FILE_OPEN_IF,
                                       share_delete=False)
                parent.close()
                parent = child
            yield parent
        finally:
            parent.close()
        return
    # Ancestors need traversal, not directory enumeration. Darwin's O_SEARCH
    # value is stable even when Python was built against an older SDK.
    access = getattr(os, "O_SEARCH", 0x40000000) if sys.platform == "darwin" else getattr(os, "O_PATH", os.O_RDONLY)
    flags = access | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        yield parent
    finally:
        os.close(parent)


def _open_child(parent, name, *, create=False):
    if os.name == "nt":
        import msvcrt

        win = _windows()
        with win.open_child(parent, name, 0x100003 if create else 0x1,
                            directory=False, disposition=win.FILE_CREATE if create else win.FILE_OPEN) as handle:
            flags = os.O_BINARY | os.O_NOINHERIT | (os.O_RDWR if create else os.O_RDONLY)
            return msvcrt.open_osfhandle(handle.detach(), flags)
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= os.O_RDWR | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
    return os.open(name, flags, 0o600, dir_fd=parent)


@contextmanager
def _regular(path):
    with _directory(path.parent) as parent:
        descriptor = _open_child(parent, path.name)
        with os.fdopen(descriptor, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if stat.S_ISDIR(mode):
                raise IsADirectoryError(errno.EISDIR, "Filesystem read requires a file", str(path))
            if not stat.S_ISREG(mode):
                raise PermissionError(errno.EACCES, "Filesystem operation requires a regular file", str(path))
            yield stream


def _read_bytes(path, maximum):
    with _regular(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > maximum:
            return None, size
        data = stream.read(maximum + 1)
        return (None if len(data) > maximum else data), max(size, len(data))


def _identity(info):
    return info.st_dev, info.st_ino


@contextmanager
def _mutation(path, *, create):
    """Stage on the destination filesystem; commit by replacing only its name."""
    with _directory(path.parent, create=create) as parent:
        source = None
        name = None
        stage = None
        original = None
        try:
            try:
                source = os.fdopen(_open_child(parent, path.name), "rb")
            except FileNotFoundError:
                if not create:
                    raise
            if source is not None:
                original = os.fstat(source.fileno())
                if not stat.S_ISREG(original.st_mode):
                    raise PermissionError(errno.EACCES, "Filesystem mutation requires a regular file", str(path))
            # Staging is delayed until the caller has validated the edit. Failed
            # matching never creates a temporary file or changes the target inode.
            content = io.BytesIO(b"" if create or source is None else source.read())
            if source is not None:
                source.close()
                source = None

            def commit():
                nonlocal name, stage
                name = f".virgo-write-{uuid.uuid4().hex}.tmp"
                try:
                    stage = os.fdopen(_open_child(parent, name, create=True), "w+b")
                except PermissionError as error:
                    raise PermissionError(errno.EACCES,
                                          "Atomic file mutation requires write access to the target parent directory",
                                          str(path.parent)) from error
                stage.write(content.getvalue())
                stage.flush()
                if os.name == "posix":
                    mode = stat.S_IMODE(original.st_mode) if original is not None else 0o666 & ~_umask()
                    os.fchmod(stage.fileno(), mode)
                    try:
                        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if (original is None) != (current is None) or (original is not None and
                            _identity(current) != _identity(original)):
                        raise PermissionError(errno.EACCES, "File identity changed before atomic commit", str(path))
                    if original is None:
                        # Exclusive publication does not overwrite a file that
                        # appeared after the existence check.
                        os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                        os.unlink(name, dir_fd=parent)
                    else:
                        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    win = _windows()
                    # A CRT stat uses the same native volume/index identity.
                    try:
                        with win.open_child(parent, path.name, 0x1, directory=False) as handle:
                            import msvcrt

                            descriptor = msvcrt.open_osfhandle(handle.detach(), os.O_RDONLY | os.O_BINARY)
                            try:
                                current_info = os.fstat(descriptor)
                            finally:
                                os.close(descriptor)
                    except FileNotFoundError:
                        current_info = None
                    if ((original is None) != (current_info is None) or
                            original is not None and _identity(current_info) != _identity(original)):
                        raise PermissionError(errno.EACCES, "File identity changed before atomic commit", str(path))
                    stage.close()
                    stage = None
                    with win.open_child(parent, name, 0x10000, directory=False,
                                        share_delete=False, share_write=False) as staged:
                        win.rename_child(parent, staged, path.name, replace=original is not None)
                name = None

            try:
                yield content, original is not None, commit
            finally:
                content.close()
        finally:
            if source is not None:
                source.close()
            if stage is not None:
                stage.close()
            if name is not None:
                try:
                    if os.name == "posix":
                        os.unlink(name, dir_fd=parent)
                    else:
                        _windows().remove_child(parent, name)
                except FileNotFoundError:
                    pass


def _umask():
    value = os.umask(0)
    os.umask(value)
    return value


def _walk(root, ignore, *, max_depth=None):
    """Yield no-follow file metadata; never descend into a directory alias."""
    with _directory(root) as parent:
        if os.name == "posix":
            for relative, dirs, files, descriptor in os.fwalk(".", dir_fd=parent, follow_symlinks=False):
                dirs[:] = sorted(name for name in dirs if name not in ignore)
                if max_depth is not None and len(Path(relative).parts) >= max_depth - 1:
                    dirs.clear()
                for name in files:
                    try:
                        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except (PermissionError, FileNotFoundError):
                        continue
                    if stat.S_ISREG(info.st_mode):
                        yield root / relative / name, info
        else:
            win = _windows()
            with os.scandir(root) as entries:
                entries = sorted(entries, key=lambda entry: entry.name)
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    if info.st_file_attributes & win.FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    path = root / entry.name
                    if info.st_file_attributes & win.FILE_ATTRIBUTE_DIRECTORY:
                        if entry.name not in ignore and (max_depth is None or max_depth > 1):
                            yield from _walk(path, ignore, max_depth=max_depth - 1 if max_depth else None)
                    elif stat.S_ISREG(info.st_mode):
                        with win.open_child(parent, entry.name, 0x80, directory=False):
                            yield path, info
                except (PermissionError, FileNotFoundError):
                    continue


def glob_match(path, pattern):
    segments = pattern.split("/")
    if len(segments) == 1:
        segments = ["**", pattern]
    return _match_segments(path.split("/"), segments)


def _match_segments(parts, segments):
    if not segments:
        return not parts
    head, rest = segments[0], segments[1:]
    if head == "**":
        return bool(parts) if not rest else any(_match_segments(parts[i:], rest) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match_segments(parts[1:], rest)


def _find_all(hay, needle):
    out, index = [], hay.find(needle)
    while index != -1:
        out.append(index)
        index = hay.find(needle, index + 1)
    return out


def _locate(content, old):
    spans = [(index, index + len(old)) for index in _find_all(content, old)]
    if spans:
        return spans
    normalized, needle = content.translate(_CHAR_MAP), old.translate(_CHAR_MAP)
    spans = [(index, index + len(needle)) for index in _find_all(normalized, needle)]
    if spans:
        return spans
    lines, old_lines = content.split("\n"), old.translate(_CHAR_MAP).split("\n")
    trailing = bool(old_lines) and old_lines[-1] == ""
    if trailing:
        old_lines = old_lines[:-1]
    key = [line.rstrip() for line in old_lines]
    if not key:
        return []
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    normalized = [line.translate(_CHAR_MAP).rstrip() for line in lines]
    for index in range(len(lines) - len(key) + 1):
        if normalized[index:index + len(key)] == key:
            end = starts[index + len(key)] - 1
            if trailing and end < len(content):
                end += 1
            spans.append((starts[index], end))
    return spans


def _read(args, path, cwd):
    try:
        with _regular(path) as stream:
            if path.suffix.lower() in IMAGE_TYPES:
                data = stream.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    return failure("tools.fs.large_image")
                return result(message("tools.fs.image", name=path.name),
                              image={"data": base64.b64encode(data).decode("ascii"),
                                     "mime": IMAGE_TYPES[path.suffix.lower()]})
            if b"\0" in stream.read(8192):
                return failure("tools.fs.binary", path=args["path"])
            stream.seek(0)
            offset = _positive(args.get("offset"), 1)
            limit = min(_positive(args.get("limit"), MAX_LINES), MAX_LINES)
            chunk, size, total = [], 0, 0
            stopped = False
            with io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="") as text:
                for total, line in enumerate(text, 1):
                    if total < offset or stopped or len(chunk) >= limit:
                        continue
                    length = len(line.encode("utf-8"))
                    if size + length > MAX_BYTES:
                        if not chunk:
                            return failure("tools.fs.long_line", line=total, max_kb=MAX_BYTES // 1024,
                                           path=args["path"])
                        stopped = True
                        continue
                    chunk.append(line)
                    size += length
    except FileNotFoundError:
        return failure("tools.fs.file_missing", path=args["path"])
    except IsADirectoryError:
        return failure("tools.fs.is_directory", path=args["path"])
    if offset > max(total, 1):
        return failure("tools.fs.offset", offset=offset, total=total)
    end = offset + len(chunk) - 1
    segments = ["".join(chunk) or message("tools.fs.empty")]
    if end < total:
        segments.append(message("tools.fs.page", offset=offset, end=end, total=total, next_offset=end + 1))
    return result(*segments, details={"path": str(path), "lines": [offset, end], "total": total})


def _write(args, path, cwd):
    content = args["content"].encode("utf-8")
    with _mutation(path, create=True) as (stream, existed, commit):
        stream.write(content)
        commit()
    return result(message("tools.fs.written", verb=message("tools.fs.overwrite" if existed else "tools.fs.create"),
                          path=_display(path, cwd), size=len(content)),
                  details={"path": str(path), "created": not existed})


def _edit(args, path, cwd):
    try:
        with _mutation(path, create=False) as (stream, _, commit):
            raw = stream.read().decode("utf-8")
            bom = raw.startswith(BOM)
            raw = raw[1:] if bom else raw
            crlf = "\r\n" in raw
            content = raw.replace("\r\n", "\n")
            spans = []
            for number, edit in enumerate(args["edits"], 1):
                old = (edit.get("old_text") or "").replace("\r\n", "\n")
                new = (edit.get("new_text") or "").replace("\r\n", "\n")
                if not old:
                    return failure("tools.fs.edit_empty", index=number)
                found = _locate(content, old)
                if not found:
                    return failure("tools.fs.edit_missing", index=number, path=args["path"])
                if len(found) > 1:
                    return failure("tools.fs.edit_ambiguous", index=number, count=len(found), path=args["path"])
                spans.append((*found[0], new))
            spans.sort()
            for (_, end, _), (start, _, _) in zip(spans, spans[1:], strict=False):
                if start < end:
                    return failure("tools.fs.edit_overlap")
            updated = content
            for start, end, new in reversed(spans):
                updated = updated[:start] + new + updated[end:]
            if updated == content:
                return failure("tools.fs.edit_unchanged")
            final = updated.replace("\n", "\r\n") if crlf else updated
            stream.seek(0)
            stream.write(((BOM if bom else "") + final).encode("utf-8"))
            stream.truncate()
            commit()
    except (FileNotFoundError, IsADirectoryError):
        return failure("tools.fs.file_missing", path=args["path"])
    diff = "".join(difflib.unified_diff(content.splitlines(keepends=True), updated.splitlines(keepends=True),
                                       "a/" + path.name, "b/" + path.name, n=2))
    return result(message("tools.fs.edited", path=_display(path, cwd), count=len(spans),
                          replacements=message("tools.fs.replacements" if len(spans) > 1 else "tools.fs.replacement")),
                  details={"path": str(path), "diff": diff[:20000]})


def _glob(args, path, cwd):
    try:
        hits = [(file, info.st_mtime) for file, info in _walk(path, IGNORE_DIRS)
                if glob_match(file.relative_to(path).as_posix(), args["pattern"])]
    except (FileNotFoundError, NotADirectoryError):
        return failure("tools.fs.not_directory", path=args.get("path"))
    if not hits:
        return result(message("tools.fs.no_matches"))
    hits.sort(key=lambda item: item[1], reverse=True)
    lines, size = [], 0
    for file, _ in hits[:200]:
        line = _display(file, cwd)
        if size + len(line.encode("utf-8")) + 1 > MAX_BYTES:
            break
        lines.append(line)
        size += len(line.encode("utf-8")) + 1
    segments = ["\n".join(lines)]
    if len(hits) > len(lines):
        segments.extend(["\n", message("tools.fs.more_matches", count=len(hits) - len(lines))])
    return result(*segments, details={"count": len(hits)})


def _grep(args, path, cwd):
    try:
        pattern = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error as error:
        return failure("tools.fs.regex", error=str(error))
    if path.is_dir():
        explicit = False
        files = (file for file, _ in _walk(path, IGNORE_DIRS))
    else:
        explicit = True
        files = [path]
    context = _positive(args.get("context"), 0, zero=True)
    limit = _positive(args.get("limit"), 100)
    out, matches, size = [], 0, 0
    bounded = False
    for file in files:
        if args.get("glob") and not glob_match(Path(_display(file, cwd)).as_posix(), args["glob"]):
            continue
        try:
            data, _ = _read_bytes(file, MAX_IMAGE_BYTES)
        except OSError:
            if explicit:
                raise
            continue
        if data is None or b"\0" in data[:8192]:
            continue
        lines, shown = data.decode("utf-8", errors="replace").splitlines(), set()
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            matches += 1
            if matches > limit:
                bounded = True
                break
            for number in range(max(0, index - context), min(len(lines), index + context + 1)):
                if number in shown:
                    continue
                shown.add(number)
                separator = ":" if number == index else "-"
                snippet = lines[number] if len(lines[number]) <= 500 else lines[number][:500] + "…"
                item = f"{_display(file, cwd)}{separator}{number + 1}{separator}{snippet}"
                encoded = len(item.encode("utf-8")) + 1
                if size + encoded > MAX_BYTES:
                    bounded = True
                    break
                out.append(item)
                size += encoded
            if bounded:
                break
        if bounded:
            break
    if not out:
        return result(message("tools.fs.no_matches"))
    segments = ["\n".join(out)]
    if bounded:
        segments.extend(["\n", message("tools.fs.match_limit", limit=min(matches, limit))])
    return result(*segments, details={"matches": min(matches, limit)})


def execute(request):
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("Unsupported filesystem request version")
    operation, args = request.get("operation"), request.get("arguments")
    if not isinstance(args, dict):
        raise ValueError("Filesystem arguments must be an object")
    cwd, home = request.get("cwd"), request.get("home")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not isinstance(home, str):
        raise ValueError("Filesystem request requires an absolute cwd and a home path")
    cwd = Path(_windows().normalize_path(cwd)) if os.name == "nt" else Path(cwd).resolve(strict=True)
    path = resolve(args.get("path") or ".", cwd, home,
                   follow_symlinks=args.get("follow_symlinks", True) if operation == "read_bytes" else True)
    request["_resolved_path"] = str(path)
    operations = {"read": _read, "write": _write, "edit": _edit, "glob": _glob, "grep": _grep}
    if operation in operations:
        return operations[operation](args, path, cwd)
    if operation == "read_bytes":
        maximum = min(_positive(args.get("max_bytes"), MAX_READ_BYTES), MAX_READ_BYTES)
        data, size = _read_bytes(path, maximum)
        return result(details={"path": str(path), "data": base64.b64encode(data).decode("ascii") if data is not None else None,
                               "size": size, "too_large": data is None})
    if operation == "walk_files":
        ignore = args.get("ignore", [])
        if not isinstance(ignore, list) or any(not isinstance(name, str) for name in ignore):
            raise ValueError("Walk exclusions must be a list of names")
        depth = args.get("max_depth")
        if depth is not None:
            depth = _positive(depth, 1)
        files, truncated = [], False
        for file, info in _walk(path, set(ignore), max_depth=depth):
            if len(files) == MAX_WALK_FILES:
                truncated = True
                break
            files.append({"path": str(file), "mtime": info.st_mtime, "size": info.st_size, "mode": info.st_mode})
        return result(details={"files": files, "truncated": truncated})
    raise ValueError(f"Unsupported filesystem operation: {operation}")


def _receive(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise ValueError("Incomplete filesystem protocol message")
        chunks.extend(chunk)
    return bytes(chunks)


def main():
    if sys.argv[1:] != ["--stdio"]:
        raise SystemExit("Usage: filesystem-worker --stdio")
    request = {}
    try:
        size = struct.unpack("!I", _receive(sys.stdin.buffer, 4))[0]
        if size > MAX_WIRE_BYTES:
            raise ValueError("Filesystem request exceeds 16 MiB")
        data = _receive(sys.stdin.buffer, size)
        if sys.stdin.buffer.read(1):
            raise ValueError("Filesystem request contains trailing bytes")
        request = json.loads(data)
        response = execute(request)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        # A read-only native mount denies writes with EROFS rather than the
        # EACCES/EPERM values represented by Python's PermissionError.
        denied = isinstance(error, PermissionError) or isinstance(error, OSError) and error.errno == errno.EROFS
        kind = "permission_denied" if denied else (
            "os_error" if isinstance(error, OSError) else "invalid_request")
        target = getattr(error, "filename", None)
        if target is None or not os.path.isabs(target):
            target = request.get("_resolved_path") if isinstance(request, dict) else None
        response = result(str(error), ok=False, error={
            "kind": kind, "type": type(error).__name__, "message": str(error),
            "errno": getattr(error, "errno", None), "target": target,
        })
    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_WIRE_BYTES:
        response = result("Filesystem result exceeds 16 MiB", ok=False, error={
            "kind": "invalid_request", "type": "ValueError", "message": "Filesystem result exceeds 16 MiB",
            "errno": None, "target": None,
        })
        payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("!I", len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
