"""Native, fail-closed subprocess isolation; never an approval decision.

Linux uses bubblewrap namespaces with dropped capabilities and --info-fd startup
acknowledgement. macOS uses Seatbelt (/usr/bin/sandbox-exec).
Windows uses Standard AppContainer with a session-scoped SID and single-call
OS-propagated inheritable NTFS/ReFS ACEs.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..execution_policy import filter_sandbox_environment
from ..execution_types import ExecutionCancelled as SandboxCancelled
from ..execution_types import ExecutionFailed as SandboxProcessError
from ..execution_types import ExecutionUnavailable as SandboxUnavailable
from ..execution_types import environment_overrides
from ..process import HostProcess

_RUNTIME_ROOTS = tuple(dict.fromkeys(Path(value).resolve() for value in (sys.base_prefix, sys.prefix)))


class _CommandScratch:
    """Delete only after explicit cleanup, never merely because a handle is lost.

    If native teardown cannot establish that every process exited, an implicit
    TemporaryDirectory finalizer must not remove their still-writable storage.
    """

    def __init__(self, *, base_dir: Path | None = None, prefix: str = "virgo-sandbox-"):
        self.name = tempfile.mkdtemp(prefix=prefix, dir=str(base_dir) if base_dir is not None else None)

    def cleanup(self):
        try:
            shutil.rmtree(self.name)
        except FileNotFoundError:
            pass


def platform_backend() -> tuple[str, Path]:
    if sys.platform == "win32":
        try:
            from .windows_sandbox import backend

            return backend()
        except (OSError, AttributeError) as error:
            raise SandboxUnavailable(
                f"Required Windows sandbox APIs are unavailable: {error}",
                reason="backend_unavailable",
            ) from error
    entries = ("/usr/bin/bwrap", "/bin/bwrap") if sys.platform == "linux" else (
        ("/usr/bin/sandbox-exec",) if sys.platform == "darwin" else ()
    )
    for value in entries:
        candidate = Path(value)
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return ("bubblewrap" if sys.platform == "linux" else "seatbelt"), candidate.resolve()
        except OSError:
            continue
    raise SandboxUnavailable(
        "No supported native sandbox backend is available "
        "(Linux: bubblewrap; macOS: sandbox-exec; Windows: native x64 AppContainer or WSL2).",
        reason="backend_unavailable",
    )


@dataclass(frozen=True)
class NativePolicy:
    workspace: Path
    read_only: bool
    network_allowed: bool
    private_paths: tuple[Path, ...]
    grants: tuple[tuple[Path, bool], ...]
    read_only_paths: tuple[Path, ...] = ()

    def __post_init__(self):
        if os.name == "nt":
            from .windows_files import normalize_path

            workspace = Path(normalize_path(self.workspace))
        else:
            workspace = Path(self.workspace).resolve(strict=True)
        object.__setattr__(self, "workspace", workspace)

    def describe(self) -> dict:
        return {
            "network": "allowed" if self.network_allowed else "restricted",
            "workspace": str(self.workspace),
            "read_only": self.read_only,
            "network_allowed": self.network_allowed,
            "runtime_read_dependencies": [str(path) for path in self.runtime_paths()],
            "ambient_read_access": (
                "Windows Standard AppContainer (ALL APPLICATION PACKAGES) ambient readable objects"
                if os.name == "nt" else "POSIX read-only host paths excluding private credential roots"
            ),
        }

    def private_path(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.private_paths)

    def runtime_paths(self) -> tuple[Path, ...]:
        return _RUNTIME_ROOTS

    def all_write_denies(self) -> tuple[Path, ...]:
        """All write-denied paths (such as unapproved .virgo), resolving symlink targets."""
        paths: set[Path] = set()
        for raw in self.read_only_paths:
            if self.private_path(raw):
                continue
            paths.add(raw)
            if raw.is_symlink():
                try:
                    resolved = raw.resolve(strict=True)
                    if not self.private_path(resolved):
                        paths.add(resolved)
                except OSError:
                    pass
        return tuple(sorted(paths, key=lambda item: (len(item.parts), str(item))))

    def protected_paths(self) -> tuple[Path, ...]:
        """Existing non-symlink write-denied paths."""
        existing: set[Path] = set()
        for path in self.all_write_denies():
            try:
                if path.is_symlink():
                    resolved = path.resolve(strict=True)
                    if (resolved.is_dir() or resolved.is_file()) and not self.private_path(resolved):
                        existing.add(resolved)
                elif path.exists() and (path.is_dir() or path.is_file()):
                    existing.add(path)
            except OSError:
                continue
        return tuple(sorted(existing, key=lambda item: (len(item.parts), str(item))))

    @staticmethod
    def _system_roots() -> list[Path]:
        values = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
        if sys.platform == "darwin":
            values += ["/System/Library", "/System"]
        return list(dict.fromkeys(Path(value).resolve() for value in values if Path(value).is_dir()))

    def validate(self, cwd: Path) -> Path:
        """Check the envelope before process launch."""
        try:
            if os.name == "nt":
                from .windows_sandbox import validate_policy

                return validate_policy(self, cwd)
            cwd = Path(cwd).resolve(strict=True)
            allowed_cwd_roots = (self.workspace, *(path for path, _ in self.grants))
            if not cwd.is_dir() or not any(cwd.is_relative_to(root) for root in allowed_cwd_roots):
                raise SandboxUnavailable(
                    "Command cwd is outside the originating sandbox workspace",
                    reason="invalid_cwd",
                )
            if self.private_path(cwd):
                raise SandboxUnavailable("Command cwd is inside private Virgo storage", reason="protected_path")
            if any(path.is_relative_to(self.workspace) for path in self._system_roots()):
                raise SandboxUnavailable("Workspace must not contain system mount roots", reason="unsupported_workspace")
            return cwd
        except SandboxUnavailable:
            raise
        except (OSError, RuntimeError) as error:
            raise SandboxUnavailable(f"Could not validate sandbox paths: {error}") from error

    def _linux(
        self,
        launcher: Path,
        scratch: Path,
        cwd: Path,
        info_fd: int,
        *,
        session_cache: Path | None = None,
        empty_mask_file: Path | None = None,
    ) -> list[str]:
        args = [
            str(launcher),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--cap-drop", "ALL",
        ]
        if self.network_allowed:
            args += ["--share-net"]
        args += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/run"]
        if self.network_allowed:
            for value in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
                entry = Path(value)
                if entry.exists():
                    try:
                        source = entry.resolve(strict=True)
                    except OSError:
                        continue
                    if source.is_relative_to(Path("/run")):
                        args += ["--ro-bind", str(source), str(source)]

        mounts: list[tuple[Path, bool]] = []
        if not self.read_only:
            mounts.append((self.workspace, True))
        for path, writable in self.grants:
            if path.exists():
                mounts.append((path, writable))
        mounts.sort(key=lambda item: len(item[0].parts))

        protected = self.protected_paths()
        for path, writable in mounts:
            if not any(path != prot and path.is_relative_to(prot) for prot in protected):
                args += ["--bind" if writable else "--ro-bind", str(path), str(path)]
        for prot in protected:
            args += ["--ro-bind", str(prot), str(prot)]
        for path, writable in mounts:
            if any(path != prot and path.is_relative_to(prot) for prot in protected):
                args += ["--bind" if writable else "--ro-bind", str(path), str(path)]

        expanded_private: list[Path] = []
        for raw in self.private_paths:
            try:
                if not raw.exists() and not raw.is_symlink():
                    continue
                target = raw.resolve(strict=True)
                if not target.is_relative_to(Path("/run")):
                    expanded_private.append(target)
            except OSError:
                continue
        covered: list[Path] = []
        for path in sorted(dict.fromkeys(expanded_private), key=lambda item: len(item.parts)):
            if any(path.is_relative_to(root) for root in covered):
                continue
            if path.is_dir():
                args += ["--perms", "000", "--tmpfs", str(path), "--remount-ro", str(path)]
                covered.append(path)
            elif path.is_file() and empty_mask_file is not None:
                args += ["--ro-bind", str(empty_mask_file), str(path)]
                covered.append(path)

        if session_cache is not None:
            args += ["--bind", str(session_cache), str(session_cache)]
        args += [
            "--bind", str(scratch), str(scratch),
            "--chdir", str(cwd),
            "--info-fd", str(info_fd),
            "--",
        ]
        return args

    def _macos(self, launcher: Path, scratch: Path, *, session_cache: Path | None = None) -> list[str]:
        def quoted(path):
            return json.dumps(str(path), ensure_ascii=False)

        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            '(allow system-mac-syscall (mac-policy-name "vnguard"))',
            '(allow system-mac-syscall (require-all (mac-policy-name "Sandbox") (mac-syscall-number 67)))',
            '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
            '(allow file-write-data (literal "/dev/null"))',
            '(allow file-read* file-map-executable (subpath "/"))',
            f"(allow file-write* (subpath {quoted(scratch)}))",
        ]
        if session_cache is not None:
            lines.append(f"(allow file-write* (subpath {quoted(session_cache)}))")
        if not self.read_only:
            lines.append(f"(allow file-write* (subpath {quoted(self.workspace)}))")

        write_denies = self.all_write_denies()
        for path, writable in self.grants:
            if writable and not any(path != d and path.is_relative_to(d) for d in write_denies):
                scope = "subpath" if (not path.exists() or path.is_dir()) else "literal"
                lines.append(f"(allow file-write* ({scope} {quoted(path)}))")
        for path in write_denies:
            scope = "literal" if (path.exists() and not path.is_dir() and not path.is_symlink()) else "subpath"
            lines.append(f"(deny file-write* ({scope} {quoted(path)}))")
        for path, writable in self.grants:
            if writable and any(path != d and path.is_relative_to(d) for d in write_denies):
                scope = "subpath" if (not path.exists() or path.is_dir()) else "literal"
                lines.append(f"(allow file-write* ({scope} {quoted(path)}))")

        if self.network_allowed:
            lines.extend([
                '(allow network-outbound (remote tcp) (remote udp) '
                '(literal "/private/var/run/mDNSResponder") (control-name "com.apple.netsrc"))',
                "(allow network-bind network-inbound (local tcp) (local udp))",
            ])

        unix_roots = [scratch]
        if session_cache is not None:
            unix_roots.append(session_cache)
        if not self.read_only:
            unix_roots.append(self.workspace)
        for root in unix_roots:
            lines.append(f"(allow network-bind (local unix-socket (subpath {quoted(root)})))")
            lines.append(f"(allow network-outbound (remote unix-socket (subpath {quoted(root)})))")

        for path in self.private_paths:
            scope = "literal" if (path.exists() and path.is_file() and not path.is_symlink()) else "subpath"
            lines.append(f"(deny file-read* file-map-executable file-write* ({scope} {quoted(path)}))")
        ancestors = {parent for path in (*self.protected_paths(), *self.private_paths) for parent in path.parents}
        for path in sorted(ancestors, key=str):
            lines.append(f"(deny file-write-unlink (literal {quoted(path)}))")
        return [str(launcher), "-p", "\n".join(lines)]

    def spawn(
        self,
        argv: list[str] | tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        stdout,
        cancellation=None,
        *,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env_overrides: dict[str, str] | None = None,
        session_pool=None,
        posix_session_dir: Path | None = None,
        ensure_probed=None,
    ) -> subprocess.Popen:
        if cancellation is not None and cancellation.is_set():
            raise SandboxCancelled("Sandbox launch cancelled")
        argv = list(argv)
        if not argv or not all(isinstance(part, str) for part in argv) or not argv[0]:
            raise SandboxUnavailable("Sandbox launch requires a nonempty command argv", reason="invalid_argv")
        explicit_env = environment_overrides(env_overrides or {})
        cwd = self.validate(cwd)
        backend, launcher = platform_backend()
        if backend == "windows-appcontainer":
            from .windows_sandbox import spawn

            return spawn(
                self, argv, cwd, env, stdout, cancellation, stdin=stdin,
                stderr=stderr, env_overrides=explicit_env, session_pool=session_pool,
            )
        if launcher.is_relative_to(self.workspace):
            raise SandboxUnavailable("Workspace must not contain the sandbox launcher")
        scratch = _CommandScratch()
        scratch_path = Path(scratch.name).resolve()
        info_read = info_write = None
        proc = None
        acknowledged = False
        try:
            if self.private_path(scratch_path) or any(
                scratch_path.is_relative_to(root) for root in self.all_write_denies()
            ):
                raise SandboxUnavailable("The command scratch directory conflicts with a filesystem deny rule")

            session_cache: Path | None = None
            empty_mask_file: Path | None = None
            if posix_session_dir is not None:
                resolved_session = posix_session_dir.resolve()
                session_cache = resolved_session / "cache"
                session_cache.mkdir(parents=True, exist_ok=True)
                empty_mask_file = resolved_session / ".empty"
                if not empty_mask_file.exists():
                    empty_mask_file.write_bytes(b"")
                    os.chmod(empty_mask_file, 0o000)
            else:
                empty_mask_file = scratch_path / ".empty"
                empty_mask_file.write_bytes(b"")
                os.chmod(empty_mask_file, 0o000)

            clean_env = filter_sandbox_environment(env)
            try:
                real_home = Path.home().resolve(strict=True)
                use_real_home = real_home.is_dir() and not self.private_path(real_home)
            except OSError:
                real_home = scratch_path / "home"
                use_real_home = False
            if use_real_home:
                sandbox_home = real_home
            else:
                sandbox_home = scratch_path / "home"
                sandbox_home.mkdir(parents=True, exist_ok=True)

            cache_target = session_cache if session_cache is not None else (scratch_path / "cache")
            cache_target.mkdir(parents=True, exist_ok=True)
            clean_env.update(
                XDG_CACHE_HOME=str(cache_target),
                UV_CACHE_DIR=str(cache_target / "uv"),
                PIP_CACHE_DIR=str(cache_target / "pip"),
                NPM_CONFIG_CACHE=str(cache_target / "npm"),
            )
            clean_env.update(explicit_env)
            clean_env.update(
                HOME=str(sandbox_home),
                TMPDIR=str(scratch_path),
                TMP=str(scratch_path),
                TEMP=str(scratch_path),
                VIRGO="1",
            )

            if cancellation is not None and cancellation.is_set():
                raise SandboxCancelled("Sandbox launch cancelled before payload start")

            if backend == "bubblewrap":
                info_read, info_write = os.pipe()
                prefix = self._linux(
                    launcher, scratch_path, cwd, info_write,
                    session_cache=session_cache, empty_mask_file=empty_mask_file,
                )
                proc = subprocess.Popen(
                    [*prefix, *argv],
                    cwd=cwd,
                    env=clean_env,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    pass_fds=(info_write,),
                )
                proc._virgo_sandbox_tmp = scratch
                proc._virgo_sandbox = {**self.describe(), "backend": backend}
                os.close(info_write)
                info_write = None

                startup = bytearray()
                deadline = time.monotonic() + 5
                while True:
                    if cancellation is not None and cancellation.is_set():
                        raise SandboxProcessError(
                            "Sandbox launch cancelled while awaiting startup acknowledgement; "
                            "the command may have run. Do not replay it."
                        )
                    if time.monotonic() >= deadline:
                        raise SandboxProcessError(
                            "bubblewrap did not confirm command startup; execution is unknown and "
                            "the command may have produced effects. Do not replay it."
                        )
                    if not select.select([info_read], [], [], 0.05)[0]:
                        continue
                    chunk = os.read(info_read, 4096)
                    if not chunk:
                        break
                    startup.extend(chunk)
                    if len(startup) > 65536:
                        raise SandboxProcessError(
                            "bubblewrap returned an oversized startup acknowledgement; "
                            "command execution is unknown. Do not replay it."
                        )
                    if b"child-pid" in startup and b"}" in startup:
                        try:
                            parsed = json.loads(startup.decode("utf-8"))
                            if isinstance(parsed, dict) and "child-pid" in parsed:
                                break
                        except ValueError:
                            pass
                if b"child-pid" not in startup:
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                    code = proc.returncode
                    cleanup_process(proc)
                    proc = None
                    raise SandboxUnavailable(
                        f"bubblewrap could not establish isolation (exit {code})",
                        reason="backend_unavailable",
                    )
                acknowledged = True
                return proc

            if ensure_probed is not None:
                ensure_probed(launcher)
            prefix = self._macos(launcher, scratch_path, session_cache=session_cache)
            proc = subprocess.Popen(
                [*prefix, *argv],
                cwd=cwd,
                env=clean_env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            proc._virgo_sandbox_tmp = scratch
            proc._virgo_sandbox = {**self.describe(), "backend": backend}
            acknowledged = True
            return proc
        except (OSError, subprocess.TimeoutExpired) as error:
            if isinstance(error, SandboxCancelled):
                raise
            if proc is not None:
                raise SandboxProcessError(
                    f"Native sandbox launch reporting failed after process creation: {error}. "
                    "Command execution is unknown; do not replay it."
                ) from error
            raise SandboxUnavailable(f"Could not launch native sandbox: {error}") from error
        finally:
            startup_error = sys.exception()
            for descriptor in (info_read, info_write):
                if descriptor is not None:
                    os.close(descriptor)
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
                else:
                    scratch.cleanup()


def cleanup_process(proc) -> None:
    scratch = getattr(proc, "_virgo_sandbox_tmp", None)
    if scratch is not None:
        target = HostProcess(proc)
        try:
            target.cleanup_tree()
        except BaseException:
            if getattr(target, "tree_drained", False):
                scratch.cleanup()
                proc._virgo_sandbox_tmp = None
            raise
        scratch.cleanup()
        proc._virgo_sandbox_tmp = None
