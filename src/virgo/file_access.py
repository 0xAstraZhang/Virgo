"""Application-level Broker file operations enforced by PermissionPolicy.

Guardrail model (no retained directory-handle TOCTOU chains, no hardlink scans):
- Broker resolves paths so approval dialogs show real paths and symlinks cannot
  escape read/write rules.
- Reads open the resolved final component with O_NOFOLLOW and verify S_ISREG.
- Writes/edits do not follow a target symlink, open existing files with
  O_NOFOLLOW, and commit via atomic temp-file replacement in the target's parent.
- Single-file grants work directly without requiring a parent-directory grant.
"""
from __future__ import annotations

import errno
import io
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .execution import FileAccessDenied
from .execution_policy import PermissionPolicy, lexical_path

MAX_WALK_FILES = 100_000


def resolve_read_path(raw: str | Path, cwd: Path) -> Path:
    """Resolve a read/search path to its real filesystem location."""
    return lexical_path(raw, cwd).resolve()


def resolve_write_path(raw: str | Path, cwd: Path) -> Path:
    """Resolve ancestor symlinks while preserving the final path component."""
    lex = lexical_path(raw, cwd)
    if lex.parent == lex or not lex.name:
        return lex.resolve()
    return lex.parent.resolve() / lex.name


def check_read(path: Path, policy: PermissionPolicy) -> None:
    if policy.unrestricted:
        return
    if policy.denied(path, "read"):
        raise FileAccessDenied(
            f"Sandbox read denied for protected resource: {path}",
            dimension="protected_path", target=path,
        )
    if not policy.allows(path, "read"):
        raise FileAccessDenied(
            f"Sandbox read is outside the permitted filesystem scope: {path}",
            dimension="read_paths", target=path,
        )


def check_write(path: Path, policy: PermissionPolicy) -> None:
    if policy.unrestricted:
        return
    if policy.denied(path, "write"):
        raise FileAccessDenied(
            f"Protected resource is not writable: {path}",
            dimension="protected_path", target=path,
        )
    if not policy.allows(path, "write"):
        raise FileAccessDenied(
            f"No write grant for {path}",
            dimension="write_paths", target=path,
        )


def read_bytes(path: Path, policy: PermissionPolicy, *,
               max_bytes: int | None = None, tail: bool = False) -> bytes | tuple[bytes, int] | None:
    """Read a regular file through a final-component O_NOFOLLOW open."""
    resolved = path.resolve()
    check_read(resolved, policy)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(resolved, flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileAccessDenied(
                f"Cannot open symbolic link or non-file target: {resolved}",
                target=resolved,
            ) from error
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(errno.EISDIR, "Is a directory", str(resolved))
        if not stat.S_ISREG(info.st_mode):
            raise FileAccessDenied(f"Read requires a regular file: {resolved}", target=resolved)
        if not tail and max_bytes is not None and info.st_size > max_bytes:
            return None
        if tail:
            if max_bytes is None:
                data = stream.read()
            else:
                stream.seek(max(0, info.st_size - max_bytes))
                data = stream.read(max_bytes)
            return data, info.st_size
        return stream.read() if max_bytes is None else stream.read(max_bytes + 1)


@contextmanager
def open_mutation(path: Path, policy: PermissionPolicy, *, create: bool,
                  max_read_bytes: int | None = None):
    """Yield (stream, existed, commit) for an atomic, no-follow file mutation.

    For restricted policies, target symlinks are always rejected (never followed),
    and existing files are opened with O_NOFOLLOW. Atomic replacement via a
    temporary file in the target's parent directory also ensures hardlinked files
    never mutate external link targets and single-file grants work without a
    parent-directory grant.
    """
    target = path if path.parent == path or not path.name else path.parent.resolve() / path.name
    check_write(target, policy)

    # Reject target symlinks for restricted policies (S3-01).
    if not policy.unrestricted:
        try:
            linfo = os.lstat(target)
        except FileNotFoundError:
            linfo = None
        else:
            if stat.S_ISLNK(linfo.st_mode):
                raise FileAccessDenied(
                    f"Refusing to modify symbolic link target: {target}",
                    dimension="protected_path", target=target,
                )

    existed = False
    existing_mode: int | None = None
    initial = b""

    open_flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
                  | (0 if policy.unrestricted else getattr(os, "O_NOFOLLOW", 0)))
    try:
        fd = os.open(target, open_flags)
    except FileNotFoundError:
        if not create:
            raise
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR} and not policy.unrestricted:
            raise FileAccessDenied(
                f"Refusing to modify symbolic link target: {target}",
                dimension="protected_path", target=target,
            ) from error
        raise
    else:
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if stat.S_ISDIR(info.st_mode):
                raise IsADirectoryError(errno.EISDIR, "Is a directory", str(target))
            if not stat.S_ISREG(info.st_mode):
                raise FileAccessDenied(f"File mutation requires a regular file: {target}", target=target)
            existed = True
            existing_mode = stat.S_IMODE(info.st_mode)
            if not create:
                if max_read_bytes is not None and info.st_size > max_read_bytes:
                    raise ValueError(f"File exceeds maximum editable size ({max_read_bytes} bytes): {target}")
                initial = source.read() if max_read_bytes is None else source.read(max_read_bytes + 1)
                if max_read_bytes is not None and len(initial) > max_read_bytes:
                    raise ValueError(f"File exceeds maximum editable size ({max_read_bytes} bytes): {target}")

    buffer = io.BytesIO(initial)

    def commit():
        data = buffer.getvalue()
        if create:
            target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check symlink immediately before replacement for restricted policies.
        if not policy.unrestricted:
            try:
                if stat.S_ISLNK(os.lstat(target).st_mode):
                    raise FileAccessDenied(
                        f"Refusing to modify symbolic link target: {target}",
                        dimension="protected_path", target=target,
                    )
            except FileNotFoundError:
                pass
        # For unrestricted Host mode editing an existing symlink, preserve legacy
        # host behavior by writing through to the symlink's resolved destination.
        destination = target.resolve() if (policy.unrestricted and target.is_symlink()) else target
        fd_tmp, tmp_name = tempfile.mkstemp(prefix=".virgo-tmp-", dir=destination.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd_tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
            if existing_mode is not None:
                try:
                    os.chmod(tmp_path, existing_mode)
                except OSError:
                    pass
            os.replace(tmp_path, destination)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    try:
        yield buffer, existed, commit
    finally:
        buffer.close()


def walk_files(path: Path, policy: PermissionPolicy, ignore: set[str], *,
               max_depth: int | None = None, max_files: int = MAX_WALK_FILES):
    """Yield (Path, stat_result | None) under ``path`` within read policy and file budget."""
    root_path = path.resolve()
    check_read(root_path, policy)
    yielded = 0
    for current_str, dirnames, filenames in os.walk(root_path, followlinks=False):
        current = Path(current_str)
        kept = []
        for name in sorted(dirnames):
            if name in ignore:
                continue
            child = current / name
            if not policy.unrestricted:
                try:
                    if child.is_symlink():
                        continue
                    if policy.denied(child, "read") or not policy.allows(child, "read"):
                        continue
                except OSError:
                    continue
            kept.append(name)
        dirnames[:] = kept
        if max_depth is not None:
            depth = len(current.relative_to(root_path).parts)
            if depth >= max_depth - 1:
                dirnames.clear()
        for name in sorted(filenames):
            if yielded >= max_files:
                return
            file = current / name
            if not policy.unrestricted:
                try:
                    resolved = file.resolve()
                except OSError:
                    resolved = file
                if policy.denied(resolved, "read"):
                    continue
            try:
                info = file.lstat() if not policy.unrestricted else file.stat()
            except OSError:
                info = None
            yielded += 1
            yield file, info
