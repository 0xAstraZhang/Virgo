"""Launch a Windows CI command under a pure non-Administrators Standard User account.

Used by .github/workflows/windows-sandbox.yml to prove native Windows Standard
AppContainer isolation passes under a pure *Users-only Standard User token
(TokenElevationType == 1 / Default, IsUserAnAdmin() == False, S-1-5-32-544
absent from TokenGroups), not only under elevated runneradmin or UAC split tokens.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes as w
from pathlib import Path

if sys.platform != "win32":  # pragma: no cover - Windows CI helper
    raise SystemExit("ci/run_as_standard_user.py requires Windows")

HANDLE = ctypes.c_void_p
PSID = ctypes.c_void_p
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32

LOGON_WITH_PROFILE = 0x00000001
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

TOKEN_QUERY = 0x0008
TOKEN_GROUPS_CLASS = 2
TOKEN_ELEVATION_TYPE_CLASS = 18
TOKEN_ELEVATION_CLASS = 20
TOKEN_ELEVATION_TYPE_DEFAULT = 1
ADMINISTRATORS_SID = "S-1-5-32-544"


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", DWORD)]


class _TokenGroups(ctypes.Structure):
    _fields_ = [("GroupCount", DWORD), ("Groups", _SidAndAttributes * 1)]


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = HANDLE
_kernel32.CloseHandle.argtypes = [HANDLE]
_kernel32.CloseHandle.restype = BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p
_kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
_kernel32.WaitForSingleObject.restype = DWORD
_kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
_kernel32.GetExitCodeProcess.restype = BOOL
_kernel32.TerminateProcess.argtypes = [HANDLE, w.UINT]
_kernel32.TerminateProcess.restype = BOOL
_kernel32.ResumeThread.argtypes = [HANDLE]
_kernel32.ResumeThread.restype = DWORD
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
_kernel32.CreateJobObjectW.restype = HANDLE
_kernel32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD]
_kernel32.SetInformationJobObject.restype = BOOL
_kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
_kernel32.AssignProcessToJobObject.restype = BOOL
_kernel32.TerminateJobObject.argtypes = [HANDLE, w.UINT]
_kernel32.TerminateJobObject.restype = BOOL

_advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
_advapi32.OpenProcessToken.restype = BOOL
_advapi32.GetTokenInformation.argtypes = [
    HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    DWORD,
    ctypes.POINTER(DWORD),
]
_advapi32.GetTokenInformation.restype = BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(w.LPWSTR)]
_advapi32.ConvertSidToStringSidW.restype = BOOL
_advapi32.CreateProcessWithLogonW.argtypes = [
    w.LPCWSTR,
    w.LPCWSTR,
    w.LPCWSTR,
    DWORD,
    w.LPCWSTR,
    w.LPWSTR,
    DWORD,
    ctypes.c_void_p,
    w.LPCWSTR,
    ctypes.POINTER(_StartupInfoW),
    ctypes.POINTER(_ProcessInformation),
]
_advapi32.CreateProcessWithLogonW.restype = BOOL

_shell32.IsUserAnAdmin.argtypes = []
_shell32.IsUserAnAdmin.restype = BOOL


def _token_info(token: HANDLE, info_class: int) -> ctypes.Array[ctypes.c_char]:
    size = DWORD()
    _advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = ctypes.create_string_buffer(size.value)
    if not _advapi32.GetTokenInformation(token, info_class, buf, len(buf), ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf


def _sid_to_str(sid_ptr: PSID) -> str:
    out = w.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return out.value
    finally:
        _kernel32.LocalFree(out)


def inspect_current_token() -> dict[str, object]:
    """Return elevation status and Administrators group membership for the current process."""
    token = HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        elev = DWORD.from_buffer(_token_info(token, TOKEN_ELEVATION_CLASS)).value
        elev_type = DWORD.from_buffer(_token_info(token, TOKEN_ELEVATION_TYPE_CLASS)).value
        groups_buf = _token_info(token, TOKEN_GROUPS_CLASS)
        count = DWORD.from_buffer(groups_buf).value
        if count > 0:
            entries = (_SidAndAttributes * count).from_buffer(groups_buf, _TokenGroups.Groups.offset)
            group_sids = [_sid_to_str(entries[i].Sid) for i in range(count)]
        else:
            group_sids = []
        return {
            "username": os.environ.get("USERNAME"),
            "is_user_an_admin": bool(_shell32.IsUserAnAdmin()),
            "token_elevation": int(elev),
            "token_elevation_type": int(elev_type),
            "has_administrators_sid": ADMINISTRATORS_SID in group_sids,
        }
    finally:
        _kernel32.CloseHandle(token)


def assert_standard_user() -> int:
    info = inspect_current_token()
    print(json.dumps(info, indent=2))
    failures: list[str] = []
    if info["is_user_an_admin"]:
        failures.append("IsUserAnAdmin() must be False")
    if info["token_elevation"] != 0:
        failures.append(f"TokenElevation must be 0, got {info['token_elevation']}")
    if info["token_elevation_type"] != TOKEN_ELEVATION_TYPE_DEFAULT:
        failures.append(
            f"TokenElevationType must be {TOKEN_ELEVATION_TYPE_DEFAULT} (pure Standard User), "
            f"got {info['token_elevation_type']}"
        )
    if info["has_administrators_sid"]:
        failures.append(f"TokenGroups must not contain {ADMINISTRATORS_SID}")
    if failures:
        raise SystemExit("Standard user token check failed: " + "; ".join(failures))
    return 0


def launch_as_user(
    username: str,
    password: str,
    command: list[str],
    cwd: Path,
    *,
    domain: str = ".",
    log_file: Path | None = None,
    timeout_s: float = 1200.0,
) -> int:
    cmdline = subprocess.list2cmdline(command)
    cmd_buf = ctypes.create_unicode_buffer(cmdline)
    startup = _StartupInfoW()
    startup.cb = ctypes.sizeof(startup)
    proc_info = _ProcessInformation()

    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _JobExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise ctypes.WinError(err)

    ok = _advapi32.CreateProcessWithLogonW(
        username,
        domain,
        password,
        LOGON_WITH_PROFILE,
        None,
        cmd_buf,
        CREATE_NO_WINDOW | CREATE_SUSPENDED,
        None,  # Let Windows construct the target user's own profile environment block.
        str(cwd),
        ctypes.byref(startup),
        ctypes.byref(proc_info),
    )
    if not ok:
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise ctypes.WinError(err)

    try:
        if not _kernel32.AssignProcessToJobObject(job, proc_info.hProcess):
            err = ctypes.get_last_error()
            _kernel32.TerminateProcess(proc_info.hProcess, 1)
            raise ctypes.WinError(err)
        if _kernel32.ResumeThread(proc_info.hThread) == 0xFFFFFFFF:
            err = ctypes.get_last_error()
            _kernel32.TerminateJobObject(job, 1)
            raise ctypes.WinError(err)

        wait_ms = max(1, int(timeout_s * 1000))
        status = _kernel32.WaitForSingleObject(proc_info.hProcess, wait_ms)
        if status == WAIT_TIMEOUT:
            _kernel32.TerminateJobObject(job, 124)
            _kernel32.WaitForSingleObject(proc_info.hProcess, 5000)
            rc = 124
        elif status == WAIT_OBJECT_0:
            code = DWORD()
            if not _kernel32.GetExitCodeProcess(proc_info.hProcess, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            rc = int(code.value)
        else:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel32.TerminateJobObject(job, 1)
        _kernel32.CloseHandle(job)
        _kernel32.CloseHandle(proc_info.hThread)
        _kernel32.CloseHandle(proc_info.hProcess)

    if log_file is not None and log_file.exists():
        sys.stdout.write(log_file.read_text(encoding="utf-8", errors="replace"))
        sys.stdout.flush()
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-token", action="store_true")
    parser.add_argument("--assert-standard-user", action="store_true")
    parser.add_argument("--username")
    parser.add_argument("--domain", default=".")
    parser.add_argument("--password-env", default="VIRGO_STD_PASSWORD")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.inspect_token:
        print(json.dumps(inspect_current_token(), indent=2))
        return 0
    if args.assert_standard_user:
        return assert_standard_user()

    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not args.username or not cmd:
        parser.error("--username and a command after '--' are required")
    password = os.environ.get(args.password_env)
    if not password:
        parser.error(f"Environment variable {args.password_env} is unset or empty")
    return launch_as_user(
        args.username,
        password,
        cmd,
        args.cwd.resolve(),
        domain=args.domain,
        log_file=args.log_file,
        timeout_s=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
