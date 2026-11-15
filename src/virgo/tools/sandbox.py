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
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MODES = ("disabled", "read-only", "workspace-write")
PROTECTED_NAMES = (".git", ".virgo", "AGENTS.md", "CLAUDE.md")
_ENV_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"})

# This fixed trampoline runs inside the actual envelope, with isolated Python
# startup. The status descriptor closes only on successful exec; setup/exec
# failures cannot be confused with arbitrary user output or command exit codes.
_EXEC_HANDSHAKE = """import os, sys
fd = int(sys.argv[1])
os.set_inheritable(fd, False)
os.write(fd, b'\\0')
try:
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
except BaseException as error:
    os.write(fd, str(error).encode('utf-8', errors='replace')[:1500])
    os._exit(127)
"""


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

    def runtime_paths(self) -> tuple[Path, ...]:
        """Keep interpreter roots explicit even when a broader read mount covers them."""
        return tuple(dict.fromkeys(path for value in (sys.base_prefix, sys.prefix)
                                   for path in (Path(value).absolute(), Path(value).resolve())))

    def protected_paths(self) -> tuple[Path, ...]:
        paths = []
        for name in PROTECTED_NAMES:
            path = self.workspace / name
            if path.is_symlink():
                # A resolved metadata link must never become a new host read
                # grant, and a writable link entry would bypass target mounts.
                raise SandboxUnavailable(f"Protected sandbox metadata must not be a symbolic link: {path}")
            if path.exists():
                paths.append(path)
        paths.extend(path for path in self.runtime_paths()
                     if path != self.workspace and path.is_relative_to(self.workspace))
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _system_roots() -> list[Path]:
        values = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt"]
        if sys.platform == "darwin":
            values += ["/System/Library", "/System/Cryptexes",
                       "/System/Volumes/Preboot/Cryptexes/OS", "/Library", "/dev", "/private/etc"]
        return list(dict.fromkeys(Path(value).resolve() for value in values if Path(value).is_dir()))

    def validate(self, cwd: Path) -> Path:
        """Check the envelope before the broker creates output directories/files."""
        try:
            cwd = Path(cwd).resolve(strict=True)
            if not cwd.is_dir() or not cwd.is_relative_to(self.workspace):
                raise SandboxUnavailable("Command cwd is outside the originating sandbox workspace")
            base = Path(sys.base_prefix).resolve(strict=True)
            prefix = Path(sys.prefix).resolve(strict=True)
            if any(path.is_relative_to(self.workspace) for path in (*self._system_roots(), base)):
                raise SandboxUnavailable("Workspace must not contain a system or base interpreter installation")
            if prefix.is_relative_to(self.workspace) and (prefix == self.workspace or prefix == base):
                raise SandboxUnavailable("Only a nested virtual environment may be inside the sandbox workspace")
            for value in (sys.base_prefix, sys.prefix):
                name = Path(value).absolute()
                for alias in (*name.parents, name):
                    if alias.is_relative_to(self.workspace) and alias.is_symlink():
                        # bin/python links inside a protected venv remain usable;
                        # a writable alias of the installation directory does not.
                        raise SandboxUnavailable(f"Interpreter directory alias is writable in the workspace: {alias}")
            self.protected_paths()
            for path in (cwd / ".virgo", cwd / ".virgo" / "outputs"):
                if path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
                    raise SandboxUnavailable(f"Sandbox output directory must stay inside its workspace: {path}")
            return cwd
        except SandboxUnavailable:
            raise
        except (OSError, RuntimeError) as error:
            raise SandboxUnavailable(f"Could not validate sandbox paths: {error}") from error

    def _read_roots(self) -> list[Path]:
        roots = sorted({*self._system_roots(), *(path.resolve() for path in self.runtime_paths())},
                       key=lambda p: len(p.parts))
        # Avoid overlapping mounts, which could otherwise re-expose a writable subtree.
        result: list[Path] = []
        for root in roots:
            if not any(root.is_relative_to(parent) for parent in result):
                result.append(root)
        return result

    def _linux(self, launcher: Path, scratch: Path, cwd: Path, seccomp_fd: int,
               protected_mounts: tuple[tuple[int, Path], ...] | None = None) -> list[str]:
        args = [str(launcher), "--die-with-parent", "--new-session", "--unshare-all", "--unshare-user",
                "--disable-userns", "--cap-drop", "ALL"]
        for root in self._read_roots():
            if not root.is_relative_to(self.workspace):
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
                if not any(resolved.is_relative_to(root) for root in (*roots, self.workspace)):
                    raise SandboxUnavailable(f"Interpreter alias leaves permitted roots: {alias}")
                args += ["--symlink", str(resolved), str(alias)]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/run"]
        args += ["--bind" if self.mode == "workspace-write" else "--ro-bind",
                 str(self.workspace), str(self.workspace)]
        # Apply nested interpreter protection after the workspace bind so it
        # cannot be hidden by that broader writable mount. Callers can pin the
        # sources by descriptor to prevent replacement between check and mount.
        if protected_mounts is None:
            raise SandboxUnavailable("Protected sandbox mounts require pinned descriptors")
        for descriptor, target in protected_mounts:
            # bwrap verifies the mounted inode against this descriptor and
            # closes it before exec; a /proc/self/fd path alone is insufficient.
            args += ["--ro-bind-fd", str(descriptor), str(target)]
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
            for path in self.runtime_paths():
                lines.append(f"(deny file-write* (subpath {quoted(path)}))")
        if output_path is not None:
            # The broker owns this exact spool file; do not open the output directory.
            lines.append(f"(allow file-write-data (literal {quoted(output_path.resolve())}))")
        return [str(launcher), "-p", "\n".join(lines)]

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], stdout,
              output_path: Path | None = None, cancellation=None) -> subprocess.Popen:
        if cancellation is not None and cancellation.is_set():
            raise SandboxCancelled("Sandbox launch cancelled")
        cwd = self.validate(cwd)
        backend, launcher = platform_backend()
        if launcher.is_relative_to(self.workspace):
            raise SandboxUnavailable("Workspace must not contain the sandbox launcher")
        scratch = tempfile.TemporaryDirectory(prefix="virgo-sandbox-")
        scratch_path = Path(scratch.name).resolve()
        filter_file = None
        pinned = []
        status_read = status_write = None
        proc = None
        try:
            (scratch_path / "home").mkdir()
            clean_env = {key: value for key, value in env.items() if key in _ENV_KEYS}
            clean_env.update(HOME=str(scratch_path / "home"), TMPDIR=str(scratch_path),
                             TMP=str(scratch_path), TEMP=str(scratch_path), VIRGO="1")
            if backend == "bubblewrap":
                filter_file = _seccomp_file()
                root = os.open(self.workspace, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for path in self.protected_paths():
                        descriptor = os.dup(root)
                        try:
                            for part in path.relative_to(self.workspace).parts:
                                child = os.open(part, os.O_PATH | os.O_NOFOLLOW, dir_fd=descriptor)
                                os.close(descriptor)
                                descriptor = child
                                mode = os.fstat(descriptor).st_mode
                                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                                    raise SandboxUnavailable(f"Unsafe protected sandbox mount: {path}")
                            pinned.append((descriptor, path))
                        except BaseException:
                            os.close(descriptor)
                            raise
                finally:
                    os.close(root)
                prefix = self._linux(launcher, scratch_path, cwd, filter_file.fileno(), tuple(pinned))
                inherited = (filter_file.fileno(), *(descriptor for descriptor, _ in pinned))
            else:
                prefix = self._macos(launcher, scratch_path, output_path)
                inherited = ()
            extra = {"pass_fds": inherited}
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
            status_read, status_write = os.pipe()
            proc = subprocess.Popen([*prefix, sys.executable, "-I", "-S", "-c", _EXEC_HANDSHAKE,
                                     str(status_write), *argv], cwd=cwd, env=clean_env,
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
                                    start_new_session=True, pass_fds=(*inherited, status_write))
            proc._virgo_sandbox_tmp = scratch
            proc._virgo_sandbox = {**self.describe(), "backend": backend}
            os.close(status_write)
            status_write = None
            startup = bytearray()
            deadline = time.monotonic() + 5
            while True:
                if cancellation is not None and cancellation.is_set():
                    raise SandboxCancelled("Sandbox launch cancelled before command exec")
                if time.monotonic() >= deadline:
                    raise SandboxUnavailable(f"{backend} did not confirm command startup")
                if not select.select([status_read], [], [], 0.05)[0]:
                    continue
                chunk = os.read(status_read, 1502)
                if not chunk:
                    break
                startup.extend(chunk)
                if len(startup) > 1501:
                    raise SandboxUnavailable(f"{backend} returned an invalid startup acknowledgement")
            if startup != b"\0":
                reason = bytes(startup).lstrip(b"\0").decode("utf-8", errors="replace")
                raise SandboxUnavailable(f"{backend} failed before command exec: {reason or 'no startup acknowledgement'}")
            return proc
        except (OSError, subprocess.TimeoutExpired) as error:
            if isinstance(error, SandboxCancelled):
                raise
            raise SandboxUnavailable(f"Could not launch native sandbox: {error}") from error
        finally:
            failed = sys.exc_info()[0] is not None
            for descriptor in (status_read, status_write):
                if descriptor is not None:
                    os.close(descriptor)
            for descriptor, _ in pinned:
                os.close(descriptor)
            if filter_file is not None:
                filter_file.close()
            if failed:
                if proc is not None:
                    cleanup_process(proc)
                else:
                    scratch.cleanup()


def cleanup_process(proc) -> None:
    scratch = getattr(proc, "_virgo_sandbox_tmp", None)
    if scratch is not None:
        # The existing executor owns waiting/cancellation; reap remaining members
        # of its process group before releasing private scratch storage.
        # Darwin can report EPERM for a group containing only an unreaped zombie.
        # Reap our exited launcher before signalling, and close that exit race
        # without ignoring a permission failure against a live process group.
        proc.poll()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if proc.poll() is None:
                raise
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            proc.wait(timeout=5)
        scratch.cleanup()
        proc._virgo_sandbox_tmp = None
