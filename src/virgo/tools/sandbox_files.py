"""Descriptor-bound Core file mutations under an active POSIX sandbox policy."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .sandbox import PROTECTED_NAMES, SandboxPolicy


class SandboxFileDenied(PermissionError):
    """A file mutation cannot retain its checked workspace boundary."""


@contextmanager
def open_mutation(path: Path, policy: SandboxPolicy, *, create: bool):
    """Open a checked regular file without following replacement path entries.

    The caller holds its normal file lock. Never use O_TRUNC: an existing file
    is validated first, and edit keeps this same descriptor from read to write.
    """
    protected = (*(policy.workspace / name for name in PROTECTED_NAMES), *policy.runtime_paths())
    if (policy.mode != "workspace-write" or not path.is_relative_to(policy.workspace)
            or any(path.is_relative_to(root.resolve()) for root in protected)):
        raise SandboxFileDenied(f"Sandbox file operation denied: {path}")
    parts = path.relative_to(policy.workspace).parts
    if not parts:
        raise SandboxFileDenied("Sandbox file operation requires a file below the workspace root")

    directory = descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(policy.workspace, flags)
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        flags = (os.O_WRONLY if create else os.O_RDWR) | os.O_NOFOLLOW | os.O_NONBLOCK
        existed = True
        if create:
            try:
                descriptor = os.open(parts[-1], flags | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=directory)
                existed = False
            except FileExistsError:
                pass
        if descriptor is None:
            descriptor = os.open(parts[-1], flags, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SandboxFileDenied(f"Sandbox file operation requires a regular file: {path}")
        stream = os.fdopen(descriptor, "wb" if create else "r+b")
        descriptor = None  # The stream now owns the descriptor, including error paths.
        with stream:
            yield stream, existed
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise SandboxFileDenied(f"Sandbox file path changed or contains a symbolic link: {path}") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
