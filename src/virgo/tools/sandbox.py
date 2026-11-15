"""Native, fail-closed subprocess isolation; never an approval decision.

Linux uses a system-owned bubblewrap plus a seccomp filter. macOS uses Seatbelt.
The broker stays outside so model transport and diagnostic persistence keep their
own authority. Only explicitly selected runtime/workspace paths are exposed.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MODES = ("disabled", "read-only", "workspace-write")
PROTECTED_NAMES = (".git", ".virgo", "AGENTS.md", "CLAUDE.md")
_ENV_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"})


class SandboxCancelled(InterruptedError):
    """Cancellation arrived before the payload was launched."""


class SandboxUnavailable(RuntimeError):
    """The requested isolation could not be established; do not run natively."""


def validate_config(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("sandbox must be an object")
    if set(value) - {"mode", "network"}:
        raise ValueError("sandbox supports only mode and network")
    if value.get("mode", "disabled") not in MODES:
        raise ValueError(f"sandbox.mode must be one of {', '.join(MODES)}")
    if value.get("network", "restricted") != "restricted":
        raise ValueError("sandbox.network currently supports only restricted; no implicit escalation")


def _system_file(path: Path) -> Path:
    """Never discover a security launcher or library through workspace PATH."""
    path = path.resolve(strict=True)
    for candidate in (path, *path.parents):
        info = candidate.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SandboxUnavailable(f"Sandbox runtime is not system-owned: {candidate}")
    if not path.is_file():
        raise SandboxUnavailable(f"Sandbox runtime is not a file: {path}")
    return path


def platform_backend() -> tuple[str, Path]:
    candidates = ("/usr/bin/bwrap", "/bin/bwrap") if sys.platform == "linux" else (
        ("/usr/bin/sandbox-exec",) if sys.platform == "darwin" else ()
    )
    for value in candidates:
        try:
            path = _system_file(Path(value))
            if os.access(path, os.X_OK):
                return ("bubblewrap" if sys.platform == "linux" else "seatbelt"), path
        except FileNotFoundError:
            continue
    raise SandboxUnavailable("No supported native sandbox backend is available (Linux: system bubblewrap; macOS: sandbox-exec).")


def _seccomp_file():
    """Compile with the system libseccomp instead of hard-coding syscall numbers.

    Network namespaces alone do not block pathname Unix sockets. Deny socket
    creation and io_uring as well, so mounted service sockets cannot provide an
    out-of-sandbox execution or network route.
    """
    machine = platform.machine()
    triplet = {"x86_64": "x86_64-linux-gnu", "aarch64": "aarch64-linux-gnu"}.get(machine)
    if triplet is None:
        raise SandboxUnavailable(f"Unsupported seccomp architecture: {machine}")
    lib = None
    for value in (f"/usr/lib/{triplet}/libseccomp.so.2", f"/lib/{triplet}/libseccomp.so.2",
                  "/usr/lib64/libseccomp.so.2", "/usr/lib/libseccomp.so.2"):
        try:
            lib = ctypes.CDLL(str(_system_file(Path(value))))
            break
        except FileNotFoundError:
            continue
    if lib is None:
        raise SandboxUnavailable("A system-owned libseccomp.so.2 is required")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_export_bpf.restype = ctypes.c_int
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW; foreign architectures fail closed.
    if not context:
        raise SandboxUnavailable("Could not initialize seccomp")
    output = tempfile.TemporaryFile()
    try:
        for name in ("socket", "socketpair", "socketcall", "io_uring_setup", "io_uring_enter",
                     "io_uring_register", "ptrace", "process_vm_writev", "process_vm_readv",
                     "bpf", "perf_event_open", "mount", "umount2", "pivot_root", "setns", "unshare"):
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:  # Not a native syscall on this architecture.
                if name != "socketcall":
                    raise SandboxUnavailable(f"System libseccomp does not recognize required syscall {name}")
                continue
            if lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise SandboxUnavailable(f"Could not restrict syscall {name}")
        if lib.seccomp_export_bpf(context, output.fileno()) != 0:
            raise SandboxUnavailable("Could not export seccomp filter")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        lib.seccomp_release(context)


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str
    workspace: Path

    def __post_init__(self):
        if self.mode not in {"read-only", "workspace-write"}:
            raise ValueError("An active SandboxPolicy requires read-only or workspace-write")
        object.__setattr__(self, "workspace", Path(self.workspace).resolve(strict=True))

    def describe(self) -> dict:
        return {"mode": self.mode, "network": "restricted", "workspace": str(self.workspace)}

    def protected_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.workspace / name).resolve() for name in PROTECTED_NAMES
                                   if (self.workspace / name).exists()))

    def _read_roots(self) -> list[Path]:
        values = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt"]
        if sys.platform == "darwin":
            values += ["/System/Library", "/System/Cryptexes",
                       "/System/Volumes/Preboot/Cryptexes/OS", "/Library", "/dev", "/private/etc"]
        values += [sys.base_prefix, sys.prefix]
        roots = sorted({Path(p).resolve() for p in values if Path(p).is_dir()}, key=lambda p: len(p.parts))
        # Avoid overlapping mounts, which could otherwise re-expose a writable subtree.
        result: list[Path] = []
        for root in roots:
            if not any(root.is_relative_to(parent) for parent in result):
                result.append(root)
        return result

    def _linux(self, launcher: Path, scratch: Path, cwd: Path, seccomp_fd: int) -> list[str]:
        args = [str(launcher), "--die-with-parent", "--new-session", "--unshare-all", "--unshare-user",
                "--disable-userns", "--cap-drop", "ALL"]
        for root in self._read_roots():
            args += ["--ro-bind", str(root), str(root)]
        for alias in ("/bin", "/sbin", "/lib", "/lib64", "/etc"):
            if Path(alias).is_symlink():
                args += ["--symlink", os.readlink(alias), alias]
        # uv virtualenv executables can refer to an unversioned directory
        # alias, while base_prefix names the versioned installation. Recreate
        # only aliases resolving into already permitted runtime roots.
        roots = self._read_roots()
        pending = [Path(sys.executable), Path(sys.prefix), Path(sys.base_prefix)]
        seen = {Path(p) for p in ("/bin", "/sbin", "/lib", "/lib64", "/etc")}
        while pending:
            name = pending.pop()
            for alias in (*reversed(name.parents), name):
                if alias in seen:
                    continue
                seen.add(alias)
                if not alias.is_symlink():
                    continue
                target = Path(os.readlink(alias))
                pending.append(target if target.is_absolute() else alias.parent / target)
                if any(alias.is_relative_to(root) for root in roots):
                    continue  # The alias is already present inside a mounted root.
                resolved = alias.resolve(strict=True)
                if not any(resolved.is_relative_to(root) for root in roots):
                    raise SandboxUnavailable(f"Interpreter alias leaves permitted roots: {alias}")
                args += ["--symlink", str(resolved), str(alias)]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/run"]
        args += ["--bind" if self.mode == "workspace-write" else "--ro-bind",
                 str(self.workspace), str(self.workspace)]
        for path in self.protected_paths():
            args += ["--ro-bind", str(path), str(path)]
        args += ["--bind", str(scratch), str(scratch), "--chdir", str(cwd),
                 "--seccomp", str(seccomp_fd), "--"]
        return args

    def _macos(self, launcher: Path, scratch: Path, output_path: Path | None) -> list[str]:
        def quoted(path):
            # JSON string escaping also prevents policy injection by path spelling.
            return json.dumps(str(path), ensure_ascii=False)
        readable = (*self._read_roots(), self.workspace, scratch)
        lines = ["(version 1)", "(deny default)", "(allow process-exec)", "(allow process-fork)",
                 "(allow signal (target same-sandbox))", "(allow process-info* (target same-sandbox))",
                 "(allow sysctl-read)", "(allow file-read-metadata)",
                 '(allow system-mac-syscall (mac-policy-name "vnguard"))',
                 '(allow system-mac-syscall (require-all (mac-policy-name "Sandbox") (mac-syscall-number 67)))',
                 '(allow file-read* (literal "/"))',
                 '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
                 '(allow file-write-data (literal "/dev/null"))']
        lines += [f"(allow file-read* file-map-executable (subpath {quoted(root)}))" for root in readable]
        lines.append(f"(allow file-write* (subpath {quoted(scratch)}))")
        if self.mode == "workspace-write":
            exclusions = " ".join(f"(require-not (subpath {quoted(self.workspace / name)}))"
                                  for name in PROTECTED_NAMES)
            # Explicit exclusions also protect metadata paths which do not exist yet.
            lines.append(f"(allow file-write* (require-all (subpath {quoted(self.workspace)}) {exclusions}))")
            for name in PROTECTED_NAMES:
                path = self.workspace / name
                if path.exists() and path.resolve() != path:
                    lines.append(f"(deny file-write* (subpath {quoted(path.resolve())}))")
        if output_path is not None:
            # The broker owns this exact spool file; do not open the output directory.
            lines.append(f"(allow file-write-data (literal {quoted(output_path.resolve())}))")
        return [str(launcher), "-p", "\n".join(lines)]

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], stdout,
              output_path: Path | None = None, cancellation=None) -> subprocess.Popen:
        if cancellation is not None and cancellation.is_set():
            raise SandboxCancelled("Sandbox launch cancelled")
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_relative_to(self.workspace):
            raise SandboxUnavailable("Command cwd is outside the originating sandbox workspace")
        backend, launcher = platform_backend()
        for runtime_root in (*self._read_roots(), launcher):
            if runtime_root.is_relative_to(self.workspace):
                raise SandboxUnavailable("Workspace must not contain the sandbox or interpreter installation")
        scratch = tempfile.TemporaryDirectory(prefix="virgo-sandbox-")
        scratch_path = Path(scratch.name).resolve()
        filter_file = None
        try:
            (scratch_path / "home").mkdir()
            clean_env = {key: value for key, value in env.items() if key in _ENV_KEYS}
            clean_env.update(HOME=str(scratch_path / "home"), TMPDIR=str(scratch_path),
                             TMP=str(scratch_path), TEMP=str(scratch_path), VIRGO="1")
            if backend == "bubblewrap":
                filter_file = _seccomp_file()
                prefix = self._linux(launcher, scratch_path, cwd, filter_file.fileno())
                extra = {"pass_fds": (filter_file.fileno(),)}
            else:
                prefix = self._macos(launcher, scratch_path, output_path)
                extra = {}
            # Probe the exact envelope without invoking the user's shell or payload.
            # A launch denied by namespaces/Seatbelt is not permission to retry natively.
            true = str(_system_file(Path("/usr/bin/true")))
            probe = subprocess.run([*prefix, true], cwd=cwd, env=clean_env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5,
                                   start_new_session=True, **extra)
            if probe.returncode:
                reason = probe.stdout.decode("utf-8", errors="replace")[-1500:].strip()
                raise SandboxUnavailable(f"{backend} could not establish isolation (exit {probe.returncode}): {reason}")
            if cancellation is not None and cancellation.is_set():
                raise SandboxCancelled("Sandbox launch cancelled before payload start")
            if filter_file is not None:
                filter_file.seek(0)
            proc = subprocess.Popen([*prefix, *argv], cwd=cwd, env=clean_env,
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
                                    start_new_session=True, **extra)
            proc._virgo_sandbox_tmp = scratch
            proc._virgo_sandbox = {**self.describe(), "backend": backend}
            return proc
        except SandboxCancelled:
            scratch.cleanup()
            raise
        except (OSError, subprocess.TimeoutExpired) as error:
            scratch.cleanup()
            raise SandboxUnavailable(f"Could not launch native sandbox: {error}") from error
        except BaseException:
            scratch.cleanup()
            raise
        finally:
            if filter_file is not None:
                filter_file.close()


def cleanup_process(proc) -> None:
    scratch = getattr(proc, "_virgo_sandbox_tmp", None)
    if scratch is not None:
        # The existing executor owns waiting/cancellation; reap remaining members
        # of its process group before releasing private scratch storage.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        scratch.cleanup()
        proc._virgo_sandbox_tmp = None
