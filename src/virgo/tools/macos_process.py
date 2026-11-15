"""A transient launchd job owns each Seatbelt command's process lifetime.

The payload remains inside the existing Seatbelt policy. A trusted supervisor
bridges pinned descriptors and wait status; it does not interpret shell input.
Darwin's coalition interfaces are private SPI, so missing interfaces or an
unsuccessful detached-child capability check make this backend unavailable.
"""
from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import plistlib
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_START_TIMEOUT = 5.0
_DRAIN_TIMEOUT = 3.0
_PROBE_LIFETIME = 12
_MESSAGE_LIMIT = 1024 * 1024
_CAPABILITY_LOCK = threading.Lock()
_CAPABILITY_PID = 0
_CAPABILITY_RESULT: bool | str | None = None

# This code is supplied as a fixed -c argument, never imported from a workspace.
# The private socket is deliberately outside every payload-readable root.
_SUPERVISOR = r"""import ctypes, errno, json, os, select, signal, socket, subprocess, sys, time
address, token, target = sys.argv[1:4]
stopping = False
broker_gone = False
def stop(signum, frame):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.settimeout(5)
connection.connect(address)
def report(value):
    global broker_gone
    try:
        connection.sendall(json.dumps(value).encode() + b'\n')
    except OSError:
        broker_gone = True
        return False
    return True
library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
terminate = library.coalition_terminate
terminate.argtypes = [ctypes.c_uint64, ctypes.c_uint32]
terminate.restype = ctypes.c_int
ctypes.set_errno(0)
# ID zero cannot name a coalition. The kernel checks effective resource-
# coalition privilege before looking it up: EPERM is the required result.
unprivileged = terminate(0, 0) == -1 and ctypes.get_errno() == errno.EPERM
if not report({'token': token, 'pid': os.getpid(), 'unprivileged': unprivileged}):
    raise SystemExit(1)
marker, descriptors, flags, _ = socket.recv_fds(connection, 1, 2)
if marker != b'S' or flags & socket.MSG_CTRUNC:
    raise RuntimeError('Invalid launch descriptor message')
for descriptor in descriptors:
    os.set_inheritable(descriptor, False)
data = bytearray()
while b'\n' not in data:
    chunk = connection.recv(65536)
    if not chunk:
        raise RuntimeError('Broker left before command release')
    data.extend(chunk)
    if len(data) > 1048576:
        raise RuntimeError('Launch request is too large')
line, extra = bytes(data).split(b'\n', 1)
if extra:
    raise RuntimeError('Unexpected launch control data')
request = json.loads(line)
child = None
try:
    if request['mode'] == 'probe':
        if descriptors:
            raise RuntimeError('Unexpected probe descriptors')
        ready_read, ready_write = os.pipe()
        # The intermediate exits before cleanup. Its orphan ignores TERM and
        # has a fixed alarm, so normal child termination cannot pass this check.
        probe = '''import json, os, signal, sys, time
fd = int(sys.argv[1])
signal.alarm(12)
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.alarm(12)
os.write(fd, json.dumps({'pid': os.getpid(), 'ready_at': time.monotonic()}).encode() + b'\\n')
os.close(fd)
time.sleep(12)
os._exit(0)
'''
        try:
            child = subprocess.Popen([sys.executable, '-I', '-S', '-c', probe, str(ready_write)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=request['env'], pass_fds=(ready_write,), start_new_session=True)
            os.close(ready_write)
            ready_write = None
            if not select.select([ready_read], [], [], 3)[0]:
                raise RuntimeError('Detached capability child did not become ready')
            ready = json.loads(os.read(ready_read, 256))
            report({'probe_ready': ready['pid'], 'ready_at': ready['ready_at']})
        finally:
            os.close(ready_read)
            if ready_write is not None:
                os.close(ready_write)
    elif request['mode'] == 'command':
        if len(descriptors) != 2 or not unprivileged:
            raise RuntimeError('Unverified command launch')
        output_fd, status_fd = descriptors
        argv = request['argv']
        argv[request['status_index']] = str(status_fd)
        child = subprocess.Popen(argv, cwd=request['cwd'], env=request['env'],
            stdin=subprocess.DEVNULL, stdout=output_fd, stderr=subprocess.STDOUT,
            pass_fds=(status_fd,), start_new_session=True)
        report({'started': child.pid})
    else:
        raise RuntimeError('Unknown launch request')
except BaseException as error:
    report({'error': str(error)[:1500]})
finally:
    for descriptor in descriptors:
        os.close(descriptor)
connection.setblocking(False)
sent_result = False
term_sent = None
while True:
    code = child.poll() if child is not None else None
    if code is not None and not sent_result:
        if not broker_gone:
            report({'result': code})
        sent_result = True
    if stopping:
        if child is None or code is not None:
            break
        if term_sent is None:
            child.terminate()
            term_sent = time.monotonic()
        elif time.monotonic() - term_sent >= 0.2:
            child.kill()
    if not broker_gone:
        try:
            if select.select([connection], [], [], 0.02)[0]:
                connection.recv(1)
                broker_gone = True
        except OSError:
            broker_gone = True
    if broker_gone and not stopping:
        # The broker can disappear abruptly. Ask launchd to remove this exact
        # job using the supervisor's authority. The command never inherits the
        # private control socket; its inherited bootstrap port is sandboxed.
        stopping = True
        try:
            subprocess.run(['/bin/launchctl', 'bootout', target], timeout=2,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired):
            pass
    if broker_gone:
        time.sleep(0.02)
"""


def _unavailable(message: str):
    from .sandbox import SandboxUnavailable

    return SandboxUnavailable(message)


def _check_cancel(cancellation) -> None:
    if cancellation is not None and cancellation.is_set():
        from .sandbox import SandboxCancelled

        raise SandboxCancelled("Sandbox launch cancelled")


class _Kernel:
    def __init__(self):
        try:
            self.system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            self.proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.pidinfo = self.proc.proc_pidinfo
            self.pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
            self.pidinfo.restype = ctypes.c_int
            self.usage = self.system.coalition_info_resource_usage
            self.usage.argtypes = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
            self.usage.restype = ctypes.c_int
            self.peereid = self.system.getpeereid
            self.peereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                    ctypes.POINTER(ctypes.c_uint)]
            self.peereid.restype = ctypes.c_int
        except (OSError, AttributeError) as error:
            raise _unavailable(f"macOS coalition interfaces are unavailable: {error}") from error

    def coalition(self, pid: int) -> int:
        # proc_pidcoalitioninfo: two coalition IDs followed by three reserved
        # uint64 fields. Resource coalition is slot zero (XNU proc_info_private.h).
        info = (ctypes.c_uint64 * 5)()
        ctypes.set_errno(0)
        size = self.pidinfo(pid, 20, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or not info[0]:
            raise _unavailable(f"Could not identify macOS process coalition (pid {pid}, errno {ctypes.get_errno()})")
        return int(info[0])

    def active(self, coalition: int) -> int | None:
        # The kernel accepts a prefix of coalition_resource_usage. Counters are
        # copied under its lock; IDs increase monotonically and are reaped only
        # after active_count reaches zero. Only ESRCH means a saved ID is gone.
        counts = (ctypes.c_uint64 * 2)()
        ctypes.set_errno(0)
        if self.usage(coalition, ctypes.byref(counts), ctypes.sizeof(counts)) != 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise _unavailable(f"Could not inspect macOS coalition {coalition} (errno {error})")
        if counts[1] > counts[0]:
            raise _unavailable("Invalid macOS coalition task counters")
        return int(counts[0] - counts[1])

    def peer(self, connection: socket.socket) -> int:
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if self.peereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) or uid.value != os.getuid():
            raise _unavailable("Launchd supervisor has an unexpected peer identity")
        # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2.
        pid = connection.getsockopt(0, 2)
        if pid <= 0:
            raise _unavailable("Launchd supervisor did not provide a kernel peer PID")
        return pid


class LaunchdProcess:
    """The Popen subset used by the foreground and background shell executors."""

    @property
    def tree_drained(self) -> bool:
        return self._drained

    def __init__(self, policy, cwd: Path, env: dict[str, str]):
        if sys.platform != "darwin":
            raise _unavailable("Launchd command containment requires macOS")
        from .sandbox import _system_file

        self._launchctl = str(_system_file(Path("/bin/launchctl")))
        self._env_program = str(_system_file(Path("/usr/bin/env")))
        self._kernel = _Kernel()
        self._policy = policy
        self._cwd = policy.validate(cwd)
        self._env = dict(env)
        self._lock = threading.RLock()
        self._listener = self._connection = None
        self._buffer = bytearray()
        self._error = None
        self._eof = False
        self._registered = self._released = self._drained = False
        self._mode = None
        self._bootout_requested = False
        self._termination_requested = False
        self._coalition = None
        self._safe_coalition = False
        self._probe_pid = None
        self._probe_ready_at = None
        self._control = None
        self.pid = 0
        self.payload_pid = None
        self.returncode = None
        self.args: list[str] = []
        self.stdout = None
        self._label = "io.virgo.shell." + secrets.token_hex(16)
        self._target = f"user/{os.getuid()}/{self._label}"
        self._token = secrets.token_hex(32)
        try:
            self._control = self._private_directory()
            self._socket_path = self._control / "s"
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(str(self._socket_path))
            self._listener.listen(1)
            self._listener.settimeout(0.05)
            self._plist = self._control / "job.plist"
            descriptor = os.open(self._plist, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                plistlib.dump({
                    "Label": self._label,
                    "ProgramArguments": [self._env_program, "-i", *(f"{key}={value}" for key, value in self._env.items()),
                                         sys.executable, "-I", "-S", "-c", _SUPERVISOR,
                                         str(self._socket_path), self._token, self._target],
                    "WorkingDirectory": str(self._control),
                    "RunAtLoad": True, "KeepAlive": False, "LaunchOnlyOnce": True,
                    "ExitTimeOut": 1, "StandardInPath": "/dev/null",
                    "StandardOutPath": "/dev/null", "StandardErrorPath": str(self._control / "bootstrap.log"),
                }, stream)
        except BaseException:
            self._close_local()
            raise

    def _private_directory(self) -> Path:
        forbidden = (*self._policy._read_roots(), self._policy.workspace,
                     Path(self._env["TMPDIR"]).resolve())
        for base in dict.fromkeys((Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"))):
            directory = Path(tempfile.mkdtemp(prefix="vgo-", dir=base)).resolve()
            mode = directory.stat()
            if (mode.st_uid == os.getuid() and stat.S_IMODE(mode.st_mode) == 0o700
                    and len(os.fsencode(directory / "s")) < 104
                    and not any(directory.is_relative_to(root) for root in forbidden)):
                return directory
            shutil.rmtree(directory)
        raise _unavailable("No private launchd control directory is outside the sandbox grants")

    def _failure(self, message: str):
        if self._mode == "command" and self._released:
            from .sandbox import SandboxProcessError

            return SandboxProcessError(
                f"{message}. The command may already have executed; do not replay it."
            )
        return _unavailable(message)

    def _ctl(self, *arguments: str, timeout: float = 2.0) -> subprocess.CompletedProcess:
        return subprocess.run([self._launchctl, *arguments], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                              timeout=max(0.05, timeout), check=False)

    @staticmethod
    def _reason(result) -> str:
        return result.stdout.decode("utf-8", errors="replace")[-1500:].strip()

    def _startup_reason(self) -> str:
        try:
            with (self._control / "bootstrap.log").open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 1500))
                return stream.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return "no startup diagnostic"

    def _registered_now(self) -> bool:
        result = self._ctl("print", self._target)
        if result.returncode == 0:
            return True
        if result.returncode in (errno.ESRCH, 113):
            return False
        raise _unavailable(f"Could not inspect launchd job: {self._reason(result)}")

    def _hello(self, cancellation) -> None:
        deadline = time.monotonic() + _START_TIMEOUT
        while self._connection is None:
            _check_cancel(cancellation)
            if time.monotonic() >= deadline:
                raise _unavailable(f"Launchd supervisor did not connect: {self._startup_reason()}")
            try:
                self._connection, _ = self._listener.accept()
            except TimeoutError:
                continue
        self.pid = self._kernel.peer(self._connection)
        self._connection.settimeout(0.05)
        data = bytearray()
        while b"\n" not in data:
            _check_cancel(cancellation)
            if time.monotonic() >= deadline:
                raise _unavailable("Launchd supervisor did not authenticate")
            try:
                chunk = self._connection.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                raise _unavailable("Launchd supervisor exited before authentication")
            data.extend(chunk)
            if len(data) > 4096:
                raise _unavailable("Invalid launchd supervisor greeting")
        message = json.loads(data)
        if (not isinstance(message, dict) or message.get("token") != self._token or message.get("pid") != self.pid
                or message.get("unprivileged") is not True):
            raise _unavailable("Launchd supervisor identity or coalition privilege check failed")
        self._coalition = self._kernel.coalition(self.pid)
        if self._coalition == self._kernel.coalition(os.getpid()):
            raise _unavailable("Launchd job shares the broker's resource coalition")
        # Ordinary fork/exec/spawn inherit this identity. The unprivileged gate
        # does not describe Apple's separate private coalition-spawn entitlement.
        while self._kernel.active(self._coalition) != 1:
            _check_cancel(cancellation)
            if time.monotonic() >= deadline:
                raise _unavailable("Launchd job does not have a private resource coalition")
            time.sleep(0.02)
        self._safe_coalition = True
        self._connection.setblocking(False)

    def _launch(self, mode: str, argv, stdout, status_fd, status_arg_index, cancellation) -> None:
        _check_cancel(cancellation)
        self._mode = mode
        self.args = list(argv)
        self._registered = True  # A failed/timed-out bootstrap can still have registered the job.
        result = self._ctl("bootstrap", f"user/{os.getuid()}", str(self._plist))
        if result.returncode:
            raise _unavailable(f"Could not bootstrap private launchd job: {self._reason(result)}")
        self._hello(cancellation)
        _check_cancel(cancellation)
        request = {"mode": mode, "argv": list(argv), "cwd": str(self._cwd), "env": self._env,
                   "status_index": status_arg_index}
        data = json.dumps(request).encode() + b"\n"
        if len(data) > _MESSAGE_LIMIT:
            raise _unavailable("Sandbox command exceeds the launch protocol limit")
        owned = []
        try:
            descriptors = []
            if mode == "command":
                if stdout == subprocess.PIPE:
                    read_fd, output_fd = os.pipe()
                    self.stdout = os.fdopen(read_fd, "rb")
                    owned.append(output_fd)
                elif stdout == subprocess.DEVNULL:
                    output_fd = os.open(os.devnull, os.O_WRONLY)
                    owned.append(output_fd)
                else:
                    output_fd = 1 if stdout is None else (stdout if isinstance(stdout, int) else stdout.fileno())
                descriptors = [output_fd, status_fd]
            self._connection.settimeout(_START_TIMEOUT)
            if socket.send_fds(self._connection, [b"S"], descriptors) != 1:
                raise _unavailable("Could not transfer launch descriptors")
            # A failed send can still deliver the complete request. From this
            # point any failure must acknowledge possible command side effects.
            self._released = True
            self._connection.sendall(data)
            self._connection.setblocking(False)
        finally:
            self._connection.setblocking(False)
            for descriptor in owned:
                os.close(descriptor)

    def start(self, argv: list[str], stdout, status_fd: int, status_arg_index: int, cancellation=None):
        try:
            if (not 0 <= status_arg_index < len(argv) or argv[status_arg_index] != str(status_fd)
                    or status_fd < 0):
                raise _unavailable("Invalid sandbox exec acknowledgement descriptor")
            _ensure_capability(self._policy, self._cwd, self._env, cancellation)
            self._launch("command", argv, stdout, status_fd, status_arg_index, cancellation)
            return self
        except BaseException as error:
            if self.stdout is not None:
                self.stdout.close()
            try:
                self.cleanup_tree()
            except Exception as cleanup_error:
                raise self._failure(f"{error}; launchd cleanup failed: {cleanup_error}") from error
            if isinstance(error, Exception):
                from .sandbox import SandboxCancelled

                if not isinstance(error, SandboxCancelled):
                    raise self._failure(f"Could not launch macOS command job: {error}") from error
            raise

    def _read_updates(self) -> None:
        try:
            self._receive_updates()
        except (OSError, ValueError, TypeError) as error:
            self._error = self._error or f"Invalid launchd supervisor response: {error}"
            self._eof = True

    def _receive_updates(self) -> None:
        if self._connection is None or self._eof:
            return
        while True:
            try:
                chunk = self._connection.recv(4096)
            except BlockingIOError:
                break
            except ConnectionResetError:
                self._eof = True
                break
            if not chunk:
                self._eof = True
                break
            self._buffer.extend(chunk)
            if len(self._buffer) > 8192:
                raise ValueError("Response exceeds its size limit")
            while b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Response is not an object")
                if "result" in message and type(message["result"]) is int:
                    self.returncode = message["result"]
                elif "started" in message and type(message["started"]) is int:
                    self.payload_pid = message["started"]
                elif "probe_ready" in message and type(message["probe_ready"]) is int:
                    timestamp = message.get("ready_at")
                    if type(timestamp) not in (float, int) or not math.isfinite(timestamp):
                        raise ValueError("Probe readiness timestamp is invalid")
                    self._probe_pid = message["probe_ready"]
                    self._probe_ready_at = timestamp
                elif "error" in message:
                    self._error = str(message["error"])[:1500]
                else:
                    raise ValueError("Unexpected supervisor message")

    def poll(self):
        with self._lock:
            self._read_updates()
            if self._error:
                raise self._failure(f"Launchd supervisor failed: {self._error}")
            if (self._eof and self.returncode is None and self._released
                    and not (self._termination_requested and self._drained)):
                raise self._failure("Launchd supervisor exited without the command's wait status")
            return self.returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if self._termination_requested and self._drained:
                # launchd may terminate the supervisor before it forwards the
                # child's status. The caller already knows timeout/cancellation;
                # retain an unknown exit code rather than inventing one.
                return None
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.02)
        return self.returncode

    def _terminate_tree(self, timeout: float) -> None:
        if self._drained or not self._registered:
            return
        deadline = time.monotonic() + timeout
        if not self._safe_coalition:
            # No payload has been released. Let the fixed, bounded supervisor
            # abort rather than booting out a possibly shared coalition.
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            while self._registered_now():
                if time.monotonic() >= deadline:
                    raise _unavailable("Unreleased launchd supervisor did not leave its one-shot job")
                time.sleep(0.05)
            self._drained = True
            return
        if not self._bootout_requested:
            result = self._ctl("bootout", self._target, timeout=min(2.0, timeout))
            if result.returncode and self._registered_now():
                raise _unavailable(f"Could not remove launchd command job: {self._reason(result)}")
            self._bootout_requested = True
        while True:
            self._read_updates()
            active = self._kernel.active(self._coalition)
            if active in (0, None):
                self._read_updates()
                if not self._registered_now():
                    self._drained = True
                    return
            if time.monotonic() >= deadline:
                raise _unavailable(f"macOS command coalition still contains {active} active task(s) after bootout")
            time.sleep(0.02)

    def kill_tree(self) -> None:
        with self._lock:
            self._termination_requested = True
            try:
                self._terminate_tree(_DRAIN_TIMEOUT)
            except Exception as error:
                raise self._failure(f"Could not terminate macOS command job: {error}") from error

    def _close_local(self) -> None:
        for connection in (self._connection, self._listener):
            if connection is not None:
                connection.close()
        self._connection = self._listener = None
        if self._control is not None:
            shutil.rmtree(self._control)
            self._control = None

    def _cleanup(self, timeout: float) -> None:
        with self._lock:
            self._terminate_tree(timeout)
            self._close_local()
            if self._error:
                raise _unavailable(f"Launchd supervisor failed: {self._error}")

    def cleanup_tree(self) -> None:
        try:
            self._cleanup(_DRAIN_TIMEOUT)
        except Exception as error:
            retained = f"; control directory retained at {self._control}" if self._control is not None else ""
            raise self._failure(f"Could not clean up macOS command job: {error}{retained}") from error


def _ensure_capability(policy, cwd, env, cancellation) -> None:
    """Exercise detached cleanup once per broker, before releasing any payload."""
    global _CAPABILITY_PID, _CAPABILITY_RESULT

    while not _CAPABILITY_LOCK.acquire(timeout=0.05):
        _check_cancel(cancellation)
    try:
        _check_cancel(cancellation)
        if _CAPABILITY_PID != os.getpid():
            _CAPABILITY_PID, _CAPABILITY_RESULT = os.getpid(), None
        if _CAPABILITY_RESULT is True:
            return
        if isinstance(_CAPABILITY_RESULT, str):
            raise _unavailable(_CAPABILITY_RESULT)
        probe = LaunchdProcess(policy, cwd, env)
        failure = None
        try:
            probe._launch("probe", [], subprocess.DEVNULL, None, None, cancellation)
            deadline = time.monotonic() + _START_TIMEOUT
            while probe._probe_pid is None:
                _check_cancel(cancellation)
                probe.poll()
                if time.monotonic() >= deadline:
                    raise _unavailable("Detached launchd capability check did not become ready")
                time.sleep(0.02)
            if probe._kernel.coalition(probe._probe_pid) != probe._coalition:
                raise _unavailable("Detached child left the launchd resource coalition")
            # Timestamp comes from the fixed probe when it arms its alarm, not
            # from receipt by a potentially descheduled broker. Check after the
            # whole operation too, including launchctl and kernel-query delays.
            proof_deadline = probe._probe_ready_at + _DRAIN_TIMEOUT
            remaining = proof_deadline - time.monotonic()
            if not 0 < remaining <= _DRAIN_TIMEOUT:
                raise _unavailable("Detached capability check missed its readiness deadline")
            with probe._lock:
                probe._terminate_tree(remaining)
            if time.monotonic() >= proof_deadline:
                raise _unavailable("Detached cleanup was not proved before the probe deadline")
        except BaseException as error:
            failure = error
        finally:
            try:
                probe._cleanup(_PROBE_LIFETIME + 2)
            except Exception as error:
                failure = _unavailable(f"{failure or 'Capability cleanup failed'}; {error}")
        if failure is not None:
            from .sandbox import SandboxCancelled

            if isinstance(failure, SandboxCancelled):
                raise failure
            _CAPABILITY_RESULT = f"macOS detached-process cleanup is unavailable: {failure}"
            raise _unavailable(_CAPABILITY_RESULT) from failure
        _CAPABILITY_RESULT = True
    finally:
        _CAPABILITY_LOCK.release()
