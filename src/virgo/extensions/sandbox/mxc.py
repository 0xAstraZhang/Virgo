"""Pinned MXC executor adapter. No host fallback or permissive audit mode."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from ...execution import ExecutionCancelled, ExecutionFailed, ExecutionUnavailable

VERSION = "1.1.0"
# SHA-256 of official executors from the pinned release archive (see docs).
EXECUTOR_HASHES = {
    ("darwin", "arm64"): "83b27bffd78ae5348d93f32d7686800fe18406658608a78e727d90eacbc22ca1",
    ("linux", "arm64"): "a95e210bce1fe937d7a8d1dd7edcc40befb6eef66715b0e8d2d96d04a14c81b7",
    ("linux", "x64"): "938e177c112b83d3f74b60cbe9e7056b56344f1fade8c02590252fb0b5c8224c",
    ("win32", "arm64"): "e4e5039cd660ac50169dba7eff286dde77f0cac79e93b880679f3a6cee743865",
    ("win32", "x64"): "cb8f9253a2288bbb5e980d63216068ea3b640ebaf845ba3fccc0d3fe822f7a0d",
}
BACKENDS = {"linux": "bubblewrap", "darwin": "seatbelt", "win32": "processcontainer"}
_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "COLORTERM", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
_BOOTSTRAP = """import os
os.dup2(1,2)
import json,subprocess,sys,time
marker,nonce,cancel,argv=sys.argv[1],sys.argv[2],sys.argv[3],json.loads(sys.argv[4])
try:
    child=subprocess.Popen(argv,close_fds=True)
except OSError as e:
    with open(marker,'x',encoding='utf-8') as f: json.dump({'nonce':nonce,'started':False,'error':str(e)},f)
    sys.exit(127)
with open(marker,'x',encoding='utf-8') as f: json.dump({'nonce':nonce,'started':True},f)
while child.poll() is None:
    if os.path.exists(cancel): os._exit(125)
    time.sleep(.025)
sys.exit(child.returncode if child.returncode >= 0 else 128-child.returncode)
"""


def _network_request(directions):
    def permission(direction):
        return "allow" if direction in directions else "deny"

    return {"egress": {"default": permission("egress")},
            "ingress": {"default": permission("ingress"), "hostLoopback": permission("hostLoopback")}}


class MxcExecutor:
    def __init__(self, executable):
        if not executable:
            raise ExecutionUnavailable("Sandbox requires sandbox.executable: install the MXC 1.1.0 executor explicitly")
        original = Path(executable)
        if not original.is_absolute() or original.is_symlink():
            raise ExecutionUnavailable("MXC executor must be an absolute, non-symlink file")
        self.path = original.resolve(strict=True)
        info = self.path.stat()
        # Windows st_mode synthesizes Unix write bits; it does not describe
        # the object's DACL. Do not mistake those bits for an ACL check.
        if not stat.S_ISREG(info.st_mode) or os.name != "nt" and info.st_mode & 0o022:
            raise ExecutionUnavailable("MXC executor must be a regular file with restricted POSIX write bits")
        self.backend = BACKENDS.get(sys.platform)
        if self.backend is None:
            raise ExecutionUnavailable(f"MXC is not supported on {sys.platform}")
        self.identity = self._identity()
        architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
        if self.identity[-1] != EXECUTOR_HASHES.get((sys.platform, architecture)):
            raise ExecutionUnavailable("Executor does not match the official MXC 1.1.0 artifact for this platform")

    def _identity(self):
        info = self.path.stat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                hashlib.sha256(self.path.read_bytes()).hexdigest())

    def spawn(self, policy, argv, cwd, env, stdout, cancellation):
        if cancellation is not None and cancellation.is_set():
            raise ExecutionCancelled("Cancelled before MXC launch")
        if self.identity != self._identity():
            raise ExecutionUnavailable("MXC executable changed; restart the session to bind its identity")
        control = Path(tempfile.mkdtemp(prefix="virgo-mxc-control-")).resolve()
        scratch = Path(tempfile.mkdtemp(prefix="virgo-mxc-scratch-")).resolve()
        channel = Path(tempfile.mkdtemp(prefix="virgo-mxc-signal-")).resolve()
        nonce = secrets.token_hex(32)
        marker = scratch / "started.json"
        home = scratch / "home"
        home.mkdir()
        clean_env = {k: v for k, v in env.items() if k.upper() in _ENV_KEYS}
        clean_env.update(HOME=str(home), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch), VIRGO="1")
        # No broad default environment or inherited provider credentials.
        wrapped = [sys.executable, "-I", "-S", "-c", _BOOTSTRAP, str(marker), nonce, str(channel / "cancel"), json.dumps(argv)]
        command = subprocess.list2cmdline(wrapped) if os.name == "nt" else shlex.join(wrapped)
        readonly = [str(channel), *(p for p in policy.resources.read_paths if p not in policy.resources.write_paths), *(str(p) for p in policy.runtime_paths()),
                    *(str(p) for p in policy.protected_paths() if p.exists())]
        request = {"version": "1.0.0", "containment": self.backend,
                   "process": {"commandLine": command, "cwd": str(cwd), "env": [f"{k}={v}" for k, v in clean_env.items()],
                               "inheritDefaultEnv": False},
                   "filesystem": {"readonlyPaths": list(dict.fromkeys(readonly)),
                                  "readwritePaths": [*policy.resources.write_paths, str(scratch)],
                                  "deniedPaths": [*(str(p) for p in policy.private_paths), str(control)]},
                   "network": _network_request(policy.resources.network),
                   "fallback": {"allowDaclMutation": False}}
        if self.backend == "seatbelt":
            request["seatbelt"] = {"guiAccess": False, "keychainAccess": False}
        if self.backend == "processcontainer":
            request["processContainer"] = {"leastPrivilege": True}
        config = control / "request.json"
        config.write_text(json.dumps(request), encoding="utf-8")
        log = control / "executor.log"
        diagnostics = control / "diagnostics.log"
        try:
            if cancellation is not None and cancellation.is_set():
                raise ExecutionCancelled("Cancelled before MXC launch")
            diagnostic_stream = diagnostics.open("wb", buffering=0)
            try:
                proc = subprocess.Popen([str(self.path), "--log-file", str(log), "--config", str(config)],
                                    cwd=str(cwd), env=clean_env, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=diagnostic_stream, close_fds=True,
                                    start_new_session=os.name != "nt")
            finally:
                diagnostic_stream.close()
        except BaseException:
            shutil.rmtree(control)
            shutil.rmtree(scratch)
            shutil.rmtree(channel)
            raise
        return MxcProcess(proc, policy, control, scratch, marker, nonce, diagnostics, channel)


class MxcProcess:
    def __init__(self, proc, policy, control, scratch, marker, nonce, log, channel):
        self.proc, self.policy = proc, policy
        self.control, self.scratch, self.marker, self.nonce, self.log = control, scratch, marker, nonce, log
        self.channel = channel
        self.pid = proc.pid
        self._closed = False
        self._terminated = False
        self._virgo_boundary = policy.describe()
        self._virgo_boundary.update(state="launch_unverified", payload_started="unknown", side_effects="possible",
                                    cleanup="pending", executor_version=VERSION)

    @property
    def returncode(self):
        return self.proc.returncode

    def poll(self):
        result = self.proc.poll()
        self._observe()
        return result

    def wait(self, timeout=None):
        result = self.proc.wait(timeout=timeout)
        self._observe()
        return result

    def _observe(self):
        from .files import read_bytes

        try:
            access = replace(self.policy, resources=replace(self.policy.resources,
                              read_paths=(str(self.marker),), write_paths=()))
            data = json.loads(read_bytes(self.marker, access, max_bytes=4096) or b'{}')
        except (OSError, ValueError):
            return
        # Scratch is writable by the payload: a rewritten negative receipt
        # cannot prove nonexecution. Never downgrade observed side effects.
        if isinstance(data, dict) and data.get("nonce") == self.nonce and data.get("started") is True:
            self._virgo_boundary.update(state="established", wrapper_started=True,
                                        payload_started=True, side_effects="possible")

    def kill_tree(self):
        # Ask the executor to run its own cleanup first. Forced termination
        # cannot certify descendant cleanup and must be reported as failure.
        if self.proc.poll() is not None:
            return
        self._terminated = True
        (self.channel / "cancel").touch()
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired as error:
            self.proc.kill()
            self.proc.wait(timeout=5)
            self._virgo_boundary["cleanup"] = "failed"
            failure = ExecutionFailed("MXC did not complete cancellation; descendant cleanup is unknown")
            failure.boundary = self._virgo_boundary
            raise failure from error

    def cleanup_tree(self):
        if self._closed:
            return
        self._observe()
        if self.proc.poll() is None:
            self.kill_tree()
        # Dedicated executor diagnostics are outside all payload grants. Never
        # classify denial, launch or cleanup by matching workload stderr.
        if self.log.exists():
            for line in self.log.read_text(errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and ("warning" in record or "error" in record):
                    self._virgo_boundary["cleanup"] = "unknown"
                    error = ExecutionFailed("MXC reported a backend error or warning; diagnostic resources retained at " + str(self.control))
                    error.boundary = self._virgo_boundary
                    raise error
        if self._virgo_boundary.get("state") != "established":
            error = ExecutionFailed("MXC did not acknowledge the workload wrapper; payload/cleanup status unknown. Diagnostics: " + str(self.control))
            error.boundary = self._virgo_boundary
            raise error
        self._virgo_boundary["cleanup"] = "best_effort" if sys.platform == "darwin" else "executor_completed"
        try:
            shutil.rmtree(self.scratch)
            shutil.rmtree(self.channel)
            shutil.rmtree(self.control)
        except OSError as cause:
            self._virgo_boundary["cleanup"] = "failed"
            failure = ExecutionFailed("MXC resource cleanup failed; remaining diagnostics: " + str(self.control))
            failure.boundary = self._virgo_boundary
            raise failure from cause
        self._closed = True
