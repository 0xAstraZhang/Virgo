"""Native, fail-closed subprocess isolation; never an approval decision.

Linux uses a system-owned bubblewrap plus a seccomp filter. macOS uses Seatbelt.
Windows uses LPAC plus a separately write-restricted token and temporary ACLs.
The broker stays outside so model transport and diagnostic persistence keep their
own authority. POSIX exposes selected paths; Windows also has ambient LPAC reads.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..execution import ExecutionCancelled as SandboxCancelled
from ..execution import ExecutionFailed as SandboxProcessError
from ..execution import ExecutionUnavailable as SandboxUnavailable
from ..execution import HostProcess, environment_overrides

_ENV_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"})
_RUNTIME_ROOTS = tuple(dict.fromkeys(Path(value).resolve() for value in (sys.base_prefix, sys.prefix)))
_TRAMPOLINE_PYTHON = str(Path(sys.executable).resolve())

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


class _CommandScratch:
    """Delete only after explicit cleanup, never merely because a handle is lost.

    If native teardown cannot establish that every process exited, an implicit
    TemporaryDirectory finalizer must not remove their still-writable storage.
    """

    def __init__(self):
        self.name = tempfile.mkdtemp(prefix="virgo-sandbox-")

    def cleanup(self):
        try:
            shutil.rmtree(self.name)
        except FileNotFoundError:
            pass


def _system_file(path: Path) -> Path:
    """Never discover a security launcher or library through workspace PATH."""
    path = path.resolve(strict=True)
    for entry in (path, *path.parents):
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SandboxUnavailable(f"Sandbox runtime is not system-owned: {entry}")
    if not path.is_file():
        raise SandboxUnavailable(f"Sandbox runtime is not a file: {path}")
    return path


def _network_config_file(path: Path) -> Path:
    """Accept systemd-resolved's service-owned data without relaxing runtime trust."""
    try:
        return _system_file(path)
    except SandboxUnavailable:
        source = path.resolve(strict=True)
        if path != Path("/etc/resolv.conf") or source not in {
            Path("/run/systemd/resolve/stub-resolv.conf"), Path("/run/systemd/resolve/resolv.conf"),
        }:
            raise
    import pwd

    try:
        service_uid = pwd.getpwnam("systemd-resolve").pw_uid
    except KeyError as error:
        raise SandboxUnavailable("Resolver configuration has no known systemd-resolve owner") from error
    entry = path.lstat()
    if (entry.st_uid != 0 or not (stat.S_ISLNK(entry.st_mode) or stat.S_ISREG(entry.st_mode))
            or not stat.S_ISLNK(entry.st_mode) and entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise SandboxUnavailable(f"Resolver configuration entry is not root-controlled: {path}")
    for parent in path.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SandboxUnavailable(f"Resolver configuration entry has an unsafe parent: {parent}")
    for entry in (source, *source.parents):
        info = entry.lstat()
        owners = {0, service_uid} if entry in {source, source.parent} else {0}
        expected_type = stat.S_ISREG if entry == source else stat.S_ISDIR
        if (info.st_uid not in owners or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not expected_type(info.st_mode)):
            raise SandboxUnavailable(f"Resolver configuration has unsafe ownership, permissions or type: {entry}")
    return source


def platform_backend() -> tuple[str, Path]:
    if sys.platform == "win32":
        try:
            from .windows_sandbox import backend

            return backend()
        except (OSError, AttributeError) as error:
            raise SandboxUnavailable(f"Required Windows sandbox APIs are unavailable: {error}") from error
    entrys = ("/usr/bin/bwrap", "/bin/bwrap") if sys.platform == "linux" else (
        ("/usr/bin/sandbox-exec",) if sys.platform == "darwin" else ()
    )
    for value in entrys:
        try:
            path = _system_file(Path(value))
            if os.access(path, os.X_OK):
                return ("bubblewrap" if sys.platform == "linux" else "seatbelt"), path
        except FileNotFoundError:
            continue
    raise SandboxUnavailable("No supported native sandbox backend is available (Linux: system bubblewrap; macOS: sandbox-exec; Windows: native x64 LPAC).")


def _seccomp_file(network_allowed: bool = False):
    """Compile with the system libseccomp instead of hard-coding syscall numbers.

    Network namespaces alone do not block pathname Unix sockets. With networking
    enabled only Internet-family sockets are allowed; Unix sockets, socketpair,
    and io_uring remain blocked so a host service socket is not an implicit grant.
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
    class ArgumentComparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(ArgumentComparison)]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_export_bpf.restype = ctypes.c_int
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW; foreign architectures fail closed.
    if not context:
        raise SandboxUnavailable("Could not initialize seccomp")
    output = tempfile.TemporaryFile()
    try:
        blocked = ("socketpair", "socketcall", "io_uring_setup", "io_uring_enter",
                     "io_uring_register", "ptrace", "process_vm_writev", "process_vm_readv",
                     "bpf", "perf_event_open", "mount", "umount2", "pivot_root", "setns", "unshare")
        for name in blocked + (() if network_allowed else ("socket",)):
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:  # Not a native syscall on this architecture.
                if name != "socketcall":
                    raise SandboxUnavailable(f"System libseccomp does not recognize required syscall {name}")
                continue
            if lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise SandboxUnavailable(f"Could not restrict syscall {name}")
        if network_allowed:
            number = lib.seccomp_syscall_resolve_name(b"socket")
            if number < 0:
                raise SandboxUnavailable("System libseccomp does not recognize socket")
            # A rule may compare an argument only once. Deny every domain except
            # AF_INET and AF_INET6 with bounded equality/range rules.
            first, last = sorted((socket.AF_INET, socket.AF_INET6))
            comparisons = [(2, first), (6, last)]  # SCMP_CMP_LT, SCMP_CMP_GT
            comparisons += [(4, value) for value in range(first + 1, last)]  # SCMP_CMP_EQ
            for operator, value in comparisons:
                comparison = ArgumentComparison(0, operator, value, 0)
                if lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM, number, 1,
                                             ctypes.byref(comparison)) != 0:
                    raise SandboxUnavailable("Could not restrict non-Internet socket families")
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
class NativePolicy:
    workspace: Path
    read_only: bool
    network_allowed: bool
    private_paths: tuple[Path, ...]
    grants: tuple[tuple[Path, bool], ...]
    extra_runtime_paths: tuple[Path, ...] = ()
    read_only_paths: tuple[Path, ...] = ()
    workspace_only = True

    def __post_init__(self):
        if os.name == "nt":
            # The runtime captured this root before execution. Do not follow a
            # junction installed later and turn its target into a fresh grant.
            from .windows_files import normalize_path

            workspace = Path(normalize_path(self.workspace))
        else:
            workspace = Path(self.workspace).resolve(strict=True)
        object.__setattr__(self, "workspace", workspace)

    def describe(self) -> dict:
        return {"network": "allowed" if self.network_allowed else "restricted",
                "workspace": str(self.workspace), "read_only": self.read_only,
                "workspace_only": self.workspace_only, "network_allowed": self.network_allowed,
                "runtime_read_dependencies": [str(path) for path in self._read_roots()],
                "ambient_read_access": "Windows LPAC ambient readable objects" if os.name == "nt" else None}

    def read_roots(self) -> tuple[Path, ...]:
        return (self.workspace, *self._read_roots(), *(path for path, _ in self.grants))

    def private_path(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.private_paths)

    def runtime_paths(self) -> tuple[Path, ...]:
        """Keep interpreter roots explicit even when a broader read mount covers them."""
        return tuple(dict.fromkeys((*_RUNTIME_ROOTS, *self.extra_runtime_paths)))

    def protected_paths(self) -> tuple[Path, ...]:
        # Restrict only the intersection with an already-readable root. A deny
        # on /home must not add a read mount exposing /home's other projects.
        paths = set()
        for denied in (*self.read_only_paths, *self.runtime_paths()):
            for readable in self.read_roots():
                if denied.is_relative_to(readable):
                    paths.add(denied)
                elif readable.is_relative_to(denied):
                    paths.add(readable)
        return tuple(sorted((path for path in paths if not self.private_path(path)),
                            key=lambda path: (len(path.parts), str(path))))

    def _writable(self, path: Path) -> bool:
        allowed = (not self.read_only and path.is_relative_to(self.workspace)
                   or any(writable and path.is_relative_to(root) for root, writable in self.grants))
        return allowed and not self.private_path(path) and not any(
            path.is_relative_to(root) for root in self.protected_paths())

    @staticmethod
    def _system_roots() -> list[Path]:
        values = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
        if sys.platform == "darwin":
            values += ["/opt/homebrew", "/System/Library", "/System/Cryptexes",
                       "/System/Volumes/Preboot/Cryptexes/OS", "/Library", "/dev", "/private/etc"]
        return list(dict.fromkeys(Path(value).resolve() for value in values if Path(value).is_dir()))

    def validate(self, cwd: Path) -> Path:
        """Check the envelope before the broker creates output directories/files."""
        try:
            for path, _writable in self.grants:
                if path.resolve(strict=True) != path:
                    raise SandboxUnavailable(f"Native grant path changed: {path}")
            if os.name == "nt":
                from .windows_sandbox import validate_policy

                return validate_policy(self, cwd)
            cwd = Path(cwd).resolve(strict=True)
            if not cwd.is_dir() or self.workspace_only and not cwd.is_relative_to(self.workspace):
                raise SandboxUnavailable("Command cwd is outside the originating sandbox workspace")
            if self.private_path(cwd):
                raise SandboxUnavailable("Command cwd is inside private Virgo storage", reason="protected_path")
            if any(path.is_relative_to(self.workspace) for path in self._system_roots()):
                raise SandboxUnavailable("A workspace cannot enclose operating-system mount roots")
            for path in self.protected_paths():
                if path.resolve(strict=True) != path:
                    raise SandboxUnavailable(f"Read-only sandbox root changed: {path}")
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
               protected_mounts: tuple[tuple[int, Path], ...] | None = None,
               denied_files: tuple[tuple[int, Path], ...] = ()) -> list[str]:
        args = [str(launcher), "--die-with-parent", "--new-session", "--unshare-all", "--unshare-user",
                "--disable-userns", "--cap-drop", "ALL"]
        if self.network_allowed:
            args += ["--share-net"]
        if not self.workspace_only:
            args += ["--ro-bind" if self.read_only else "--bind", "/", "/"]
        # Build private temporary roots before exposing trusted dependencies.
        # A runtime snapshot (or interpreter) under /tmp must not be hidden by
        # a later tmpfs mount after it has already been bound read-only.
        args += ["--proc", "/proc", "--dev", "/dev"]
        if self.workspace_only:
            args += ["--tmpfs", "/tmp", "--dir", "/run"]
        else:
            args += ["--tmpfs", "/run"]
        if self.workspace_only:
            for root in self._read_roots():
                if not root.is_relative_to(self.workspace):
                    args += ["--ro-bind", str(root), str(root)]
        for alias in ("/bin", "/sbin", "/lib", "/lib64", "/etc"):
            if self.workspace_only and Path(alias).is_symlink():
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
                if alias in seen or not self.workspace_only:
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
        if self.network_allowed:
            # Resolvers commonly link /etc/resolv.conf into /run, which remains
            # private. Expose only the verified system configuration targets,
            # never the surrounding service sockets or host runtime directory.
            for value in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
                if Path(value).exists():
                    source = _network_config_file(Path(value))
                    if source.is_relative_to(Path("/run")) or (self.workspace_only and not any(
                            source.is_relative_to(root) for root in self._read_roots())):
                        args += ["--ro-bind", str(source), str(source)]
        mounts = {self.workspace, *(path for path, _ in self.grants)}
        # A protected leaf must not become writable by relocating its parent.
        # Bind writable ancestors as mount points, without making their other
        # children read-only. Apply shallow roots first to preserve child grants.
        for path in (*self.protected_paths(), *self.private_paths):
            mounts.update(parent for parent in path.parents if self._writable(parent))
        for path in sorted(mounts, key=lambda item: (len(item.parts), str(item))):
            args += ["--bind" if self._writable(path) else "--ro-bind", str(path), str(path)]
        # Apply nested interpreter protection after the workspace bind so it
        # cannot be hidden by that broader writable mount. Callers can pin the
        # sources by descriptor to prevent replacement between check and mount.
        if protected_mounts is None:
            raise SandboxUnavailable("Protected sandbox mounts require pinned descriptors")
        for descriptor, target in protected_mounts:
            # bwrap verifies the mounted inode against this descriptor and
            # closes it before exec; a /proc/self/fd path alone is insufficient.
            args += ["--ro-bind-fd", str(descriptor), str(target)]
        masks = {path: descriptor for descriptor, path in denied_files}
        covered = []
        for path in sorted(self.private_paths, key=lambda item: len(item.parts)):
            if any(path.is_relative_to(root) for root in covered):
                continue
            if not any(path.is_relative_to(root) for root in self.read_roots()):
                continue  # Already absent from this filesystem namespace.
            if path.is_symlink():
                raise SandboxUnavailable(f"Denied sandbox root is a symbolic link: {path}")
            if path.is_dir():
                # A mode-zero read-only mount denies opening the directory,
                # rather than returning an apparently successful empty listing.
                args += ["--perms", "000", "--tmpfs", str(path), "--remount-ro", str(path)]
            elif path in masks:
                args += ["--perms", "000", "--ro-bind-data", str(masks[path]), str(path)]
            else:
                raise SandboxUnavailable(f"Denied sandbox file requires an isolated mask: {path}")
            covered.append(path)
        # Bubblewrap creates mount-point parents in its empty root and /tmp.
        # Keep those synthetic directories read-only, even when the project
        # lives below /tmp. Only the explicit writable mounts and private
        # command scratch should accept new files.
        args += ["--bind", str(scratch), str(scratch), "--remount-ro", "/tmp", "--remount-ro", "/",
                 "--chdir", str(cwd),
                 "--seccomp", str(seccomp_fd), "--"]
        return args

    def _macos(self, launcher: Path, scratch: Path) -> list[str]:
        def quoted(path):
            # JSON string escaping also prevents policy injection by path spelling.
            return json.dumps(str(path), ensure_ascii=False)
        readable = (*self._read_roots(), self.workspace, scratch) if self.workspace_only else (Path("/"),)
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
        for path, writable in self.grants:
            scope = "subpath" if path.is_dir() else "literal"
            operations = "file-read* file-map-executable" + (" file-write*" if writable else "")
            lines.append(f"(allow {operations} ({scope} {quoted(path)}))")
        if not self.read_only:
            root = self.workspace if self.workspace_only else Path("/")
            lines.append(f"(allow file-write* (subpath {quoted(root)}))")
        for path in self.protected_paths():
            scope = "subpath" if path.is_dir() else "literal"
            lines.append(f"(deny file-write* ({scope} {quoted(path)}))")
        if self.network_allowed:
            # The system resolver and routing control are narrow OS exceptions,
            # also used by Chromium's Seatbelt network policy. This does not
            # authorize arbitrary pathname sockets in the workspace or home.
            lines.extend(['(allow network-outbound (remote tcp) (remote udp) '
                          '(literal "/private/var/run/mDNSResponder") (control-name "com.apple.netsrc"))',
                          "(allow network-bind network-inbound (local tcp) (local udp))"])
            lines.append('(allow file-read* (literal "/private/var/run/resolv.conf"))')
        from .macos_process import control_root

        private = (*self.private_paths, control_root())
        for path in private:
            scope = "literal" if path.is_file() else "subpath"
            lines.append(f"(deny file-read* file-map-executable file-write* ({scope} {quoted(path)}))")
        ancestors = {parent for path in (*self.protected_paths(), *private) for parent in path.parents}
        for path in sorted(ancestors, key=str):
            lines.append(f"(deny file-write-unlink (literal {quoted(path)}))")
        return [str(launcher), "-p", "\n".join(lines)]

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], stdout,
              output_path: Path | None = None, cancellation=None, *, stdin=subprocess.DEVNULL,
              stderr=subprocess.STDOUT, env_overrides=None) -> subprocess.Popen:
        if cancellation is not None and cancellation.is_set():
            raise SandboxCancelled("Sandbox launch cancelled")
        if not argv:
            raise SandboxUnavailable("Sandbox execution requires a nonempty command argv")
        cwd = self.validate(cwd)
        explicit_env = environment_overrides(env_overrides or {})
        backend, launcher = platform_backend()
        if backend == "windows-lpac":
            from .windows_sandbox import spawn

            return spawn(self, argv, cwd, env, stdout, cancellation, stdin=stdin, stderr=stderr,
                         env_overrides=explicit_env)
        if launcher.is_relative_to(self.workspace):
            raise SandboxUnavailable("Workspace must not contain the sandbox launcher")
        scratch = _CommandScratch()
        scratch_path = Path(scratch.name).resolve()
        filter_file = None
        pinned = []
        masks = []
        status_read = status_write = None
        proc = None
        acknowledged = False
        try:
            if self.private_path(scratch_path) or any(scratch_path.is_relative_to(root)
                                                     for root in self.read_only_paths):
                raise SandboxUnavailable("The command scratch directory conflicts with a filesystem deny rule")
            (scratch_path / "home").mkdir()
            clean_env = {key: value for key, value in env.items() if key in _ENV_KEYS}
            clean_env.update(explicit_env)
            clean_env.update(HOME=str(scratch_path / "home"), TMPDIR=str(scratch_path),
                             TMP=str(scratch_path), TEMP=str(scratch_path), VIRGO="1")
            if backend == "bubblewrap":
                filter_file = _seccomp_file(self.network_allowed)
                root = os.open(self.workspace, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for path in self.protected_paths():
                        if path.is_relative_to(self.workspace):
                            descriptor = os.dup(root)
                            parts = path.relative_to(self.workspace).parts
                        else:
                            descriptor = os.open(path.anchor, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
                            parts = path.parts[1:]
                        try:
                            for part in parts:
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
                for path in self.private_paths:
                    if any(path.is_relative_to(root) for root in self.read_roots()) and path.is_file():
                        masks.append((tempfile.TemporaryFile(), path))
                denied_files = tuple((stream.fileno(), path) for stream, path in masks)
                prefix = self._linux(launcher, scratch_path, cwd, filter_file.fileno(), tuple(pinned), denied_files)
                inherited = (filter_file.fileno(), *(descriptor for descriptor, _ in pinned),
                             *(descriptor for descriptor, _ in denied_files))
            else:
                prefix = self._macos(launcher, scratch_path)
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
            command = [*prefix, _TRAMPOLINE_PYTHON, "-I", "-S", "-c", _EXEC_HANDSHAKE,
                       str(status_write), *argv]
            if backend == "seatbelt" and sys.platform == "darwin":
                from .macos_process import LaunchdProcess

                proc = LaunchdProcess(self, cwd, clean_env)
                # Attach scratch before startup so every partial launch goes
                # through the same verified process-tree cleanup path.
                proc._virgo_sandbox_tmp = scratch
                proc._virgo_sandbox = {**self.describe(), "backend": backend}
                proc.start(command, stdout, status_write, len(prefix) + 5, cancellation,
                           stdin=stdin, stderr=stderr)
            else:
                proc = subprocess.Popen(command, cwd=cwd, env=clean_env,
                                        stdin=stdin, stdout=stdout, stderr=stderr,
                                        start_new_session=True, pass_fds=(*inherited, status_write))
            proc._virgo_sandbox_tmp = scratch
            proc._virgo_sandbox = {**self.describe(), "backend": backend}
            os.close(status_write)
            status_write = None
            startup = bytearray()
            deadline = time.monotonic() + 5
            while True:
                if cancellation is not None and cancellation.is_set():
                    raise SandboxProcessError(
                        "Sandbox launch cancelled while awaiting exec acknowledgement; "
                        "the command may have run. Do not replay it.")
                if time.monotonic() >= deadline:
                    raise SandboxProcessError(
                        f"{backend} did not confirm command startup; execution is unknown and "
                        "the command may have produced effects. Do not replay it.")
                if not select.select([status_read], [], [], 0.05)[0]:
                    continue
                chunk = os.read(status_read, 1502)
                if not chunk:
                    break
                startup.extend(chunk)
                if len(startup) > 1501:
                    raise SandboxProcessError(f"{backend} returned an invalid startup acknowledgement; "
                                              "command execution is unknown. Do not replay it.")
            if startup != b"\0":
                reason = bytes(startup).lstrip(b"\0").decode("utf-8", errors="replace")
                raise SandboxUnavailable(f"{backend} failed before command exec: {reason or 'no startup acknowledgement'}")
            acknowledged = True
            return proc
        except (OSError, subprocess.TimeoutExpired) as error:
            if isinstance(error, SandboxCancelled):
                raise
            if proc is not None:
                raise SandboxProcessError(f"Native sandbox launch reporting failed after process creation: {error}. "
                                          "Command execution is unknown; do not replay it.") from error
            raise SandboxUnavailable(f"Could not launch native sandbox: {error}") from error
        finally:
            startup_error = sys.exception()
            for descriptor in (status_read, status_write):
                if descriptor is not None:
                    os.close(descriptor)
            for descriptor, _ in pinned:
                os.close(descriptor)
            for stream, _ in masks:
                stream.close()
            if filter_file is not None:
                filter_file.close()
            if not acknowledged:
                if proc is not None:
                    try:
                        cleanup_process(proc)
                    except Exception as cleanup_error:
                        failure = SandboxProcessError(
                            f"Native startup failed ({startup_error}); cleanup also failed ({cleanup_error}). "
                            "The command may have run. Do not replay it."
                        )
                        failure.boundary = {"cleanup": "failed", "payload_started": "unknown", "side_effects": "possible"}
                        raise failure from cleanup_error
                    finally:
                        for name in ("stdin", "stdout", "stderr"):
                            stream = getattr(proc, name, None)
                            if stream is not None:
                                stream.close()
                else:
                    scratch.cleanup()


def cleanup_process(proc) -> None:
    scratch = getattr(proc, "_virgo_sandbox_tmp", None)
    if scratch is not None:
        cleanup_tree = getattr(proc, "cleanup_tree", None)
        if cleanup_tree is not None:
            # The macOS adapter verifies that its complete resource coalition
            # has drained before writable scratch can be removed.
            try:
                cleanup_tree()
            except BaseException:
                # A diagnostic/control-file error can follow verified drain.
                # Preserve the error without leaking already-safe scratch.
                if proc.tree_drained:
                    scratch.cleanup()
                    proc._virgo_sandbox_tmp = None
                raise
            scratch.cleanup()
            proc._virgo_sandbox_tmp = None
            return
        # The neutral process handle pins the group identity through leader
        # exit and reaps it only after the remaining members are signalled.
        HostProcess(proc).cleanup_tree()
        scratch.cleanup()
        proc._virgo_sandbox_tmp = None
