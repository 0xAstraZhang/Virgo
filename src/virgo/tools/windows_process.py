"""Windows 10+ command processes, atomically assigned to a kill-on-close Job Object.

The JOB_LIST startup attribute assigns the process before any user code can run.
Only duplicated stdin/stdout handles are inherited; the job handle never is.
This module is imported only on Windows. There is no uncontained fallback.
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import threading
import time
import weakref
from contextlib import ExitStack
from ctypes import wintypes as w

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
SIZE_T = ctypes.c_size_t
HANDLE = w.HANDLE


class BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", w.DWORD), ("Affinity", SIZE_T), ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                ("hStdError", HANDLE)]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


def _api(name, args, result=w.BOOL):
    fn = getattr(kernel, name)
    fn.argtypes, fn.restype = args, result
    return fn


CreateJob = _api("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], HANDLE)
SetJob = _api("SetInformationJobObject", [HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD])
CloseHandle = _api("CloseHandle", [HANDLE])
TerminateJob = _api("TerminateJobObject", [HANDLE, w.UINT])
GetCurrentProcess = _api("GetCurrentProcess", [], HANDLE)
DuplicateHandle = _api("DuplicateHandle", [HANDLE, HANDLE, HANDLE, ctypes.POINTER(HANDLE), w.DWORD, w.BOOL, w.DWORD])
InitializeAttributes = _api("InitializeProcThreadAttributeList", [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(SIZE_T)])
UpdateAttribute = _api("UpdateProcThreadAttribute", [ctypes.c_void_p, w.DWORD, SIZE_T, ctypes.c_void_p, SIZE_T,
                                                    ctypes.c_void_p, ctypes.c_void_p])
DeleteAttributes = _api("DeleteProcThreadAttributeList", [ctypes.c_void_p], None)
CreateProcess = _api("CreateProcessW", [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                                      ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInfo)])
WaitForSingleObject = _api("WaitForSingleObject", [HANDLE, w.DWORD], w.DWORD)
GetExitCode = _api("GetExitCodeProcess", [HANDLE, ctypes.POINTER(w.DWORD)])


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _close_handles(job, process):
    # Closing the last job handle kills descendants, including after the root exits.
    CloseHandle(job)
    CloseHandle(process)


class WindowsProcess:
    """Small Popen-compatible surface used by foreground and background commands."""

    def __init__(self, argv, *, cwd, env, stdout):
        self.args = argv
        self.returncode = None
        self.stdout = None
        self._lock = threading.RLock()
        try:
            with ExitStack() as resources, ExitStack() as ownership:
                job = _check(CreateJob(None, None))
                ownership.callback(CloseHandle, job)
                limits = ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                _check(SetJob(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
                stdin_file = resources.enter_context(open(os.devnull, "rb"))
                if stdout == subprocess.PIPE:
                    read_fd, write_fd = os.pipe()
                    self.stdout = os.fdopen(read_fd, "rb")
                    output_file = resources.enter_context(os.fdopen(write_fd, "wb", buffering=0))
                elif stdout == subprocess.DEVNULL:
                    output_file = resources.enter_context(open(os.devnull, "wb"))
                else:
                    output_file = stdout

                def inherit(file):
                    duplicate = HANDLE()
                    owner = GetCurrentProcess()
                    _check(DuplicateHandle(owner, msvcrt.get_osfhandle(file.fileno()), owner,
                                           ctypes.byref(duplicate), 0, True, 2))  # DUPLICATE_SAME_ACCESS
                    resources.callback(CloseHandle, duplicate.value)
                    return duplicate.value

                input_handle, output_handle = inherit(stdin_file), inherit(output_file)
                size = SIZE_T()
                InitializeAttributes(None, 2, 0, ctypes.byref(size))
                if not size.value:
                    raise ctypes.WinError(ctypes.get_last_error())
                attributes = ctypes.create_string_buffer(size.value)
                _check(InitializeAttributes(attributes, 2, 0, ctypes.byref(size)))
                resources.callback(DeleteAttributes, attributes)
                inherited = (HANDLE * 2)(input_handle, output_handle)
                jobs = (HANDLE * 1)(job)
                _check(UpdateAttribute(attributes, 0, 0x20002, inherited, ctypes.sizeof(inherited), None, None))
                _check(UpdateAttribute(attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None))
                startup = StartupInfoEx()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = input_handle
                startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = output_handle
                startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
                command = subprocess.list2cmdline(argv) if isinstance(argv, list) else argv
                command_buffer = ctypes.create_unicode_buffer(command)
                environment = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in
                                                                     sorted(env.items(), key=lambda item: item[0].upper())) + "\0")
                info = ProcessInfo()
                flags = 0x08000000 | 0x00080000 | 0x00000400  # NO_WINDOW | EXTENDED_STARTUPINFO | UNICODE_ENVIRONMENT
                _check(CreateProcess(None, command_buffer, None, None, True, flags, environment, str(cwd),
                                     ctypes.byref(startup), ctypes.byref(info)))
                ownership.callback(CloseHandle, info.hProcess)
                CloseHandle(info.hThread)
                self.pid = info.dwProcessId
                self._process, self._job = info.hProcess, job
                self._finalizer = weakref.finalize(self, _close_handles, job, info.hProcess)
                ownership.pop_all()
        except BaseException:
            if self.stdout is not None:
                self.stdout.close()
            raise

    def poll(self):
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            status = WaitForSingleObject(self._process, 0)
            if status == 258:  # WAIT_TIMEOUT
                return None
            if status != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            code = w.DWORD()
            _check(GetExitCode(self._process, ctypes.byref(code)))
            self.returncode = code.value
            self._finalizer()
            return self.returncode

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.01)
        return self.returncode

    def kill(self):
        with self._lock:
            if self.returncode is None:
                _check(TerminateJob(self._job, 1))

    terminate = kill

    def communicate(self, input=None, timeout=None):
        if input is not None:
            raise ValueError("Command stdin is closed")
        chunks = []
        reader = None
        if self.stdout is not None:
            reader = threading.Thread(target=lambda: chunks.append(self.stdout.read()), daemon=True)
            reader.start()
        self.wait(timeout)
        if reader is not None:
            reader.join()
            self.stdout.close()
        return (b"".join(chunks) if reader is not None else None), None
