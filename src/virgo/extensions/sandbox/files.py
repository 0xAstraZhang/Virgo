"""Sandbox broker mutations staged away from untrusted workspace entries."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from ...execution import FileAccessDenied
from .links import has_external_writable_link
from .policy import SandboxPolicy


def check_read(path: Path, policy: SandboxPolicy) -> None:
    if policy.private_path(path):
        raise FileAccessDenied(f"Sandbox read denied for private Virgo storage: {path}",
                                dimension="protected_path", target=path)
    if not any(path.is_relative_to(root) for root in policy.read_roots()):
        raise FileAccessDenied(f"Sandbox read is outside the permitted filesystem scope: {path}",
                                dimension="read_paths", target=path)


@contextmanager
def _open_directory(path: Path):
    """Retain directory identity and reject links introduced after resolution."""
    if os.name == "nt":
        from .windows_files import open_path

        with open_path(path, 0x81, directory=True, share_delete=False) as handle:
            yield handle
        return
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def read_bytes(path: Path, policy: SandboxPolicy, *, max_bytes: int | None = None,
               tail: bool = False) -> bytes | tuple[bytes, int] | None:
    """Read through a retained no-link handle; never open FIFO/device paths."""
    check_read(path, policy)
    try:
        with _open_directory(path.parent) as directory:
            if os.name == "nt":
                import msvcrt

                from .windows_files import open_child

                with open_child(directory, path.name, 0x81, directory=False) as handle:
                    descriptor = msvcrt.open_osfhandle(handle.detach(), os.O_RDONLY | os.O_BINARY)
            else:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise FileAccessDenied(f"Sandbox read requires a regular file: {path}", target=path)
                if not tail and max_bytes is not None and info.st_size > max_bytes:
                    return None
                if tail:
                    stream.seek(max(0, info.st_size - max_bytes))
                    data = stream.read(max_bytes)
                else:
                    data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise FileAccessDenied(f"File identity changed during read: {path}", target=path)
                return (data, info.st_size) if tail else data
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileAccessDenied(f"Sandbox read path changed or contains a symbolic link: {path}",
                                    target=path) from error
        raise


def walk_files(path: Path, policy: SandboxPolicy, ignore: set[str], *, max_depth=None):
    """Yield paths and no-follow metadata without traversing outside aliases."""
    check_read(path, policy)
    try:
        with _open_directory(path) as directory:
            if os.name == "posix":
                for relative, names, files, descriptor in os.fwalk(".", dir_fd=directory,
                                                                follow_symlinks=False):
                    current = path / relative
                    names[:] = sorted(name for name in names if name not in ignore
                                      and not policy.private_path((current / name).resolve()))
                    if max_depth is not None and len(Path(relative).parts) >= max_depth - 1:
                        names.clear()
                    for name in files:
                        file = current / name
                        if policy.private_path(file.resolve()):
                            continue
                        try:
                            yield file, os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
            else:
                # The retained native handle denies directory replacement. Keep
                # every parent pinned while recursively inspecting its children.
                from .windows_files import (
                    FILE_ATTRIBUTE_DIRECTORY,
                    FILE_ATTRIBUTE_REPARSE_POINT,
                    open_child,
                )

                with os.scandir(path) as entries:
                    for entry in entries:
                        file = Path(entry.path)
                        if policy.private_path(file.resolve()):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if info.st_file_attributes & FILE_ATTRIBUTE_DIRECTORY:
                            if (entry.name not in ignore and (max_depth is None or max_depth > 1)
                                    and not info.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT):
                                yield from walk_files(file, policy, ignore,
                                                      max_depth=max_depth - 1 if max_depth is not None else None)
                        else:
                            # Inspect only this final object's metadata, including
                            # file links. Content reads resolve and check scope later.
                            with open_child(directory, entry.name, 0x80, directory=False, allow_reparse=True):
                                yield file, info
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileAccessDenied(f"Sandbox search path changed or contains a symbolic link: {path}",
                                    target=path) from error
        raise


@contextmanager
def open_mutation(path: Path, policy: SandboxPolicy, *, create: bool):
    """Yield a staged stream and explicit commit; never modify an existing inode.

    The caller holds its normal file lock. A hardlinked file is accepted only
    when every link is inside the writable roots; an outside alias could mutate
    a path outside the task boundary. Unlinking an alias can make an outside
    inode appear singly linked after opening, so the native launch scan and the
    broker check both remain launch-time checks. Native commands cannot modify
    .virgo (created before every native launch), nor can Core file tools. Only
    a successful caller commits the fresh inode to its target.
    """
    policy.check_write(path)
    within_workspace = path.is_relative_to(policy.workspace)
    parts = path.relative_to(policy.workspace).parts if within_workspace else path.parts[1:]
    if not parts:
        raise FileAccessDenied("Sandbox file operation requires a file below the workspace root")

    root = directory = staging = descriptor = staged_descriptor = None
    staged_name = None
    expected_identity = None
    stream = None
    try:
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root = os.open(policy.workspace, flags)
            directory = os.dup(root) if within_workspace else os.open(path.anchor, flags)
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=directory)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            target = parts[-1]
        else:
            # Windows broker operations retain the checked file boundary.
            # Keep the application-level path checks and fresh-file staging;
            # this branch does not provide POSIX descriptor-relative traversal.
            if create:
                path.parent.mkdir(parents=True, exist_ok=True)
            target = path
        flags = ((os.O_WRONLY if create else os.O_RDWR)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_BINARY", 0))
        try:
            descriptor = os.open(target, flags, dir_fd=directory)
        except FileNotFoundError:
            if not create:
                raise
        existed = descriptor is not None
        info = os.fstat(descriptor) if existed else None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                raise FileAccessDenied(f"Sandbox file operation requires a regular file: {path}")
            if info.st_nlink != 1 and has_external_writable_link(
                    path, policy.writable_roots(), policy.protected_paths()):
                raise FileAccessDenied(
                    f"Sandbox file operation rejects a hardlink outside the writable scope: {path}",
                    target=path,
                )
            if os.name == "nt":
                from .windows_files import descriptor_identity

                expected_identity = descriptor_identity(descriptor)

        metadata = policy.workspace / ".virgo"
        if os.name == "posix":
            try:
                os.mkdir(".virgo", mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            staging = os.open(".virgo", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        else:
            metadata.mkdir(mode=0o700, exist_ok=True)
            if metadata.resolve() != metadata or metadata.is_symlink():
                raise FileAccessDenied(f"Sandbox file staging must not be an alias: {metadata}")
        name = f"mutation-{uuid.uuid4().hex}.tmp"
        staged_target = name if staging is not None else metadata / name
        # Do not briefly expose a private source with broader staging permissions.
        mode = stat.S_IMODE(info.st_mode) if info is not None else 0o666
        staged_descriptor = os.open(staged_target, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                                    mode, dir_fd=staging)
        staged_name = staged_target
        stream = os.fdopen(staged_descriptor, "w+b")
        staged_descriptor = None
        if existed and not create:
            source = os.fdopen(descriptor, "rb")
            descriptor = None
            with source:
                shutil.copyfileobj(source, stream)
            stream.seek(0)
        elif descriptor is not None:
            os.close(descriptor)
            descriptor = None

        def commit():
            nonlocal staged_name
            stream.flush()
            if info is not None:
                # File permissions are retained; ownership and extended
                # attributes are not copied. Windows restores the current
                # destination DACL only after the fresh inode leaves staging.
                if os.name == "posix":
                    os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                else:
                    os.chmod(staged_target, stat.S_IMODE(info.st_mode))
                try:
                    current = os.stat(target, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError as error:
                    raise FileAccessDenied(f"Sandbox file changed before commit: {path}") from error
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise FileAccessDenied(f"Sandbox file changed before commit: {path}")
            stream.close()  # Windows replacement requires closing the staged handle.
            if os.name == "nt":
                from .windows_files import replace_staged

                with replace_staged(staged_target, target, expected_identity=expected_identity):
                    staged_name = None
            else:
                os.replace(staged_target, target, src_dir_fd=staging, dst_dir_fd=directory)
            staged_name = None

        yield stream, existed, commit
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileAccessDenied(f"Sandbox file path changed or contains a symbolic link: {path}") from error
        raise
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            for handle in (descriptor, staged_descriptor):
                if handle is not None:
                    os.close(handle)
            try:
                if staged_name is not None:
                    os.unlink(staged_name, dir_fd=staging)
            finally:
                for handle in (staging, directory, root):
                    if handle is not None:
                        os.close(handle)
