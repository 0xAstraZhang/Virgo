"""Shared execution contracts, error types, and environment helpers.

This leaf contract module depends only on ``virgo.execution_policy`` so that
``file_access``, ``output_store``, ``process``, and ``sandbox`` stay strictly
lower-triangular and mutually independent.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .execution_policy import PermissionPolicy

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionUnavailable(RuntimeError):
    """The requested boundary could not be established before payload release."""

    def __init__(self, message, *, reason=None, dimension=None, target=None):
        super().__init__(message)
        self.reason, self.dimension, self.target = reason, dimension, target


class ExecutionFailed(RuntimeError):
    """Execution/control failed after release; effects may already exist."""


class ExecutionCancelled(InterruptedError):
    pass


class FileAccessDenied(PermissionError):
    def __init__(self, message, *, dimension="filesystem", target=None):
        super().__init__(message)
        self.dimension = dimension
        self.target = str(target) if target is not None else None


class ProcessHandle(Protocol):
    returncode: int | None
    stdin: object
    stdout: object
    stderr: object

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill_tree(self) -> None: ...
    def cleanup_tree(self) -> None: ...


@dataclass(frozen=True)
class ProcessRequest:
    """An argv-based process operation; permission decisions are supplied separately."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdout: object = subprocess.PIPE
    stdin: object = subprocess.DEVNULL
    stderr: object = subprocess.STDOUT
    env_overrides: dict[str, str] = field(default_factory=dict)


class ExecutionContext(Protocol):
    provider: str
    workspace: Path
    policy: PermissionPolicy
    outputs: Any

    def check_read(self, path: Path) -> None: ...
    def check_write(self, path: Path) -> None: ...
    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes | None: ...
    def open_mutation(self, path: Path, *, create: bool, max_read_bytes: int | None = None): ...
    def walk_files(self, path: Path, ignore: set[str], *, max_depth: int | None = None,
                   max_files: int | None = None, respect_gitignore: bool = False): ...
    def open_output(self, cwd: Path, prefix: str): ...
    def run_process(self, request: ProcessRequest, cancellation=None) -> ProcessHandle: ...
    def describe(self) -> dict: ...


def environment_overrides(values: dict[str, str]) -> dict[str, str]:
    """Validate explicit adapter grants without letting them alter launcher setup.

    Ambient credentials are still filtered by native backends. These additions
    must come from trusted configuration, never a model-supplied environment.
    """
    reserved = {
        "HOME", "TMPDIR", "TMP", "TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "ENV", "BASH_ENV",
    }
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str) or not _ENV_NAME.fullmatch(name):
            raise ExecutionUnavailable(
                "Explicit environment grants require valid identifier names and string values",
                reason="invalid_environment",
            )
        if any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in value):
            raise ExecutionUnavailable(
                f"Explicit environment value for {name} contains invalid control characters",
                reason="invalid_environment",
            )
        if name.upper() in reserved or name.upper().startswith(("VIRGO", "LD_", "DYLD_", "PYTHON")):
            raise ExecutionUnavailable(
                f"Explicit environment cannot replace launcher variable {name}",
                reason="invalid_environment",
            )
    return dict(values)


def _merge_environment(
    base: dict[str, str],
    overrides: dict[str, str],
    *,
    case_insensitive: bool = os.name == "nt",
) -> dict[str, str]:
    if not overrides:
        return dict(base)
    if case_insensitive:
        override_keys = {key.upper() for key in overrides}
        merged = {key: value for key, value in base.items() if key.upper() not in override_keys}
        merged.update(overrides)
        return merged
    return {**base, **overrides}


def cleanup_process(proc) -> None:
    cleanup = getattr(proc, "cleanup_tree", None)
    if cleanup is not None:
        cleanup()
