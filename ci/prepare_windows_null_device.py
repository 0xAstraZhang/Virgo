"""CI-only helper aligning GitHub Actions Windows Server \\Device\\Null with Win11 Feature 60692039.

Windows 11 servicing builds with updated null.sys (10.0.26100.8972+, WIL Feature 60692039)
natively grant ALL APPLICATION PACKAGES (S-1-15-2-1) and ALL RESTRICTED APPLICATION PACKAGES
(S-1-15-2-2) 0x001201BF on \\Device\\Null. Stock Windows Server 2022/2025 CI runners omit
those ACEs.

Virgo production code never mutates \\Device\\Null: unsupported Windows hosts default to
No-Limit with WSL2 / No-Limit / Update Windows guidance and fail closed on explicit sandbox
modes. This CI helper exists solely to:
  1. Verify that stock Windows Server runners actually trigger the unsupported-OS fallback
     and fail-closed precheck (--verify-unsupported).
  2. Apply Microsoft's exact null.sys 10.0.26100.9818 ACEs onto \\Device\\Null on the CI
     runner so the full Standard AppContainer isolation suite (including NUL/DEVNULL and
     pure non-Admin Standard User acceptance) can execute end to end.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import struct
import sys
import tempfile
from ctypes import wintypes as w
from pathlib import Path

DEVICE_PATH = r"\\?\GLOBALROOT\Device\Null"
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
DACL_SECURITY_INFORMATION = 0x00000004
SE_DACL_PROTECTED = 0x1000
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

ACCESS_ALLOWED_ACE_TYPE = 0
NULL_SYS_APPCONTAINER_MASK = 0x001201BF  # Exact mask written by Microsoft null.sys 10.0.26100.9818
NULL_REQUIRED_MASK = 0x0012019F  # FILE_GENERIC_READ | FILE_GENERIC_WRITE
TARGET_SIDS = ("S-1-15-2-1", "S-1-15-2-2")


def _init_win32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    handle_t = ctypes.c_void_p
    psid_t = ctypes.c_void_p
    pacl_t = ctypes.c_void_p
    psd_t = ctypes.c_void_p
    dword_t = ctypes.c_uint32
    word_t = ctypes.c_uint16
    bool_t = ctypes.c_int32

    def api(dll, name, argtypes, restype=bool_t):
        fn = getattr(dll, name)
        fn.argtypes = argtypes
        fn.restype = restype
        return fn

    return {
        "HANDLE": handle_t,
        "PSID": psid_t,
        "PACL": pacl_t,
        "DWORD": dword_t,
        "WORD": word_t,
        "BOOL": bool_t,
        "INVALID_HANDLE": ctypes.c_void_p(-1).value,
        "CreateFileW": api(
            kernel32,
            "CreateFileW",
            [w.LPCWSTR, dword_t, dword_t, ctypes.c_void_p, dword_t, dword_t, handle_t],
            handle_t,
        ),
        "CloseHandle": api(kernel32, "CloseHandle", [handle_t]),
        "LocalFree": api(kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
        "ConvertStringSidToSidW": api(advapi32, "ConvertStringSidToSidW", [w.LPCWSTR, ctypes.POINTER(psid_t)]),
        "IsValidSid": api(advapi32, "IsValidSid", [psid_t]),
        "GetLengthSid": api(advapi32, "GetLengthSid", [psid_t], dword_t),
        "GetKernelObjectSecurity": api(
            advapi32,
            "GetKernelObjectSecurity",
            [handle_t, dword_t, psd_t, dword_t, ctypes.POINTER(dword_t)],
        ),
        "SetKernelObjectSecurity": api(advapi32, "SetKernelObjectSecurity", [handle_t, dword_t, psd_t]),
        "GetSecurityDescriptorDacl": api(
            advapi32,
            "GetSecurityDescriptorDacl",
            [psd_t, ctypes.POINTER(bool_t), ctypes.POINTER(pacl_t), ctypes.POINTER(bool_t)],
        ),
        "GetSecurityDescriptorControl": api(
            advapi32,
            "GetSecurityDescriptorControl",
            [psd_t, ctypes.POINTER(word_t), ctypes.POINTER(dword_t)],
        ),
        "InitializeSecurityDescriptor": api(advapi32, "InitializeSecurityDescriptor", [psd_t, dword_t]),
        "SetSecurityDescriptorDacl": api(advapi32, "SetSecurityDescriptorDacl", [psd_t, bool_t, pacl_t, bool_t]),
        "SetSecurityDescriptorControl": api(advapi32, "SetSecurityDescriptorControl", [psd_t, word_t, word_t]),
    }


def _sid_bytes(win, sid_str: str) -> bytes:
    ptr = win["PSID"]()
    if not win["ConvertStringSidToSidW"](sid_str, ctypes.byref(ptr)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not win["IsValidSid"](ptr):
            raise ValueError(f"Invalid SID: {sid_str}")
        length = win["GetLengthSid"](ptr)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(ptr, length)
    finally:
        win["LocalFree"](ptr)


def _ace_sid(ace: bytes) -> bytes | None:
    if len(ace) < 8:
        return None
    ace_type = ace[0]
    if ace_type in {0, 1, 2, 3, 9, 10, 13, 14}:
        offset = 8
    elif ace_type in {5, 6, 7, 8, 11, 12, 15, 16} and len(ace) >= 12:
        flags = struct.unpack_from("<I", ace, 8)[0]
        offset = 12 + (16 if flags & 1 else 0) + (16 if flags & 2 else 0)
    else:
        return None
    if offset + 8 > len(ace) or ace[offset] != 1:
        return None
    end = offset + 8 + 4 * ace[offset + 1]
    return ace[offset:end] if end <= len(ace) else None


def _read_null_dacl(win, handle):
    needed = win["DWORD"]()
    win["GetKernelObjectSecurity"](handle, DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = ctypes.create_string_buffer(needed.value)
    if not win["GetKernelObjectSecurity"](handle, DACL_SECURITY_INFORMATION, buf, len(buf), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    present = win["BOOL"]()
    defaulted = win["BOOL"]()
    acl_ptr = win["PACL"]()
    if not win["GetSecurityDescriptorDacl"](
        buf, ctypes.byref(present), ctypes.byref(acl_ptr), ctypes.byref(defaulted)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    control = win["WORD"]()
    sd_rev = win["DWORD"]()
    if not win["GetSecurityDescriptorControl"](buf, ctypes.byref(control), ctypes.byref(sd_rev)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not present.value or not acl_ptr.value:
        return None, control.value, bool(defaulted.value), []
    acl_rev, _, acl_size, ace_count, _ = struct.unpack("<BBHHH", ctypes.string_at(acl_ptr.value, 8))
    raw = ctypes.string_at(acl_ptr.value, acl_size)
    aces: list[bytes] = []
    pos = 8
    for _ in range(ace_count):
        if pos + 4 > len(raw):
            raise OSError("Malformed DACL on \\Device\\Null")
        size = struct.unpack_from("<H", raw, pos + 2)[0]
        if size < 4 or pos + size > len(raw):
            raise OSError("Malformed ACE on \\Device\\Null")
        aces.append(raw[pos : pos + size])
        pos += size
    return acl_rev, control.value, bool(defaulted.value), aces


def _has_aap_access(win, aces: list[bytes], sid_raw: bytes) -> bool:
    for ace in aces:
        if ace[0] == ACCESS_ALLOWED_ACE_TYPE and not (ace[1] & 0x08) and _ace_sid(ace) == sid_raw:
            mask = struct.unpack_from("<I", ace, 4)[0]
            if (mask & NULL_REQUIRED_MASK) == NULL_REQUIRED_MASK:
                return True
    return False


def check_null_device() -> bool:
    win = _init_win32()
    handle = win["CreateFileW"](DEVICE_PATH, READ_CONTROL, 0x7, None, 3, 0, None)
    if not handle or handle == win["INVALID_HANDLE"]:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        acl_rev, _, _, aces = _read_null_dacl(win, handle)
        if acl_rev is None:
            return True
        aap = _sid_bytes(win, "S-1-15-2-1")
        return _has_aap_access(win, aces, aap)
    finally:
        win["CloseHandle"](handle)


def prepare_null_device() -> None:
    win = _init_win32()
    handle = win["CreateFileW"](DEVICE_PATH, READ_CONTROL | WRITE_DAC, 0x7, None, 3, 0, None)
    if not handle or handle == win["INVALID_HANDLE"]:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        acl_rev, control, defaulted, aces = _read_null_dacl(win, handle)
        if acl_rev is None:
            print(r"\Device\Null has a NULL DACL; no change needed.")
            return
        target_bytes = [_sid_bytes(win, sid) for sid in TARGET_SIDS]
        if all(_has_aap_access(win, aces, raw) for raw in target_bytes):
            print(r"\Device\Null already grants S-1-15-2-1 and S-1-15-2-2 (Win11 Feature 60692039 parity).")
            return
        target_set = set(target_bytes)
        retained = [ace for ace in aces if _ace_sid(ace) not in target_set]
        added = [
            struct.pack("<BBHI", ACCESS_ALLOWED_ACE_TYPE, 0, 8 + len(raw), NULL_SYS_APPCONTAINER_MASK) + raw
            for raw in target_bytes
        ]
        new_aces = retained + added
        total_len = 8 + sum(len(ace) for ace in new_aces)
        acl_buf = ctypes.create_string_buffer(
            struct.pack("<BBHHH", acl_rev, 0, total_len, len(new_aces), 0) + b"".join(new_aces)
        )
        sd_buf = ctypes.create_string_buffer(64)
        if not win["InitializeSecurityDescriptor"](sd_buf, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        if not win["SetSecurityDescriptorDacl"](sd_buf, True, acl_buf, defaulted):
            raise ctypes.WinError(ctypes.get_last_error())
        if not win["SetSecurityDescriptorControl"](sd_buf, 0x1500, control & 0x1500):
            raise ctypes.WinError(ctypes.get_last_error())
        protection = (
            PROTECTED_DACL_SECURITY_INFORMATION
            if (control & SE_DACL_PROTECTED)
            else UNPROTECTED_DACL_SECURITY_INFORMATION
        )
        if not win["SetKernelObjectSecurity"](handle, DACL_SECURITY_INFORMATION | protection, sd_buf):
            raise ctypes.WinError(ctypes.get_last_error())
        _, _, _, verified = _read_null_dacl(win, handle)
        if not all(_has_aap_access(win, verified, raw) for raw in target_bytes):
            raise RuntimeError(r"Failed to verify S-1-15-2-1 / S-1-15-2-2 ACEs on \Device\Null")
        print(
            r"Prepared \Device\Null with Microsoft null.sys 10.0.26100.9818 ACEs "
            "(S-1-15-2-1 and S-1-15-2-2, mask 0x001201BF)."
        )
    finally:
        win["CloseHandle"](handle)


def verify_unsupported_fallback() -> None:
    """Verify real stock unsupported-Windows behavior before CI prepares \\Device\\Null."""
    if check_null_device():
        print(
            r"Runner natively grants S-1-15-2-1 on \Device\Null (Win11 Feature 60692039); "
            "skipping pre-prep unsupported-OS check."
        )
        return

    from virgo.execution import ExecutionUnavailable, default_execution_mode, is_windows_sandbox_supported
    from virgo.harness.session import AgentSession
    from virgo.harness.ui import UI
    from virgo.sandbox.windows_sandbox import precheck_windows_sandbox

    assert is_windows_sandbox_supported() is False
    assert default_execution_mode() == "no-limit"

    class _CaptureUI(UI):
        def __init__(self):
            self.lines: list[str] = []

        def print(self, text="", style=None):
            self.lines.append(str(text))

    with tempfile.TemporaryDirectory(prefix="virgo-unsupported-win-") as tmp:
        root = Path(tmp)
        home = root / "home"
        workspace = root / "proj"
        home.mkdir()
        workspace.mkdir()
        previous = os.environ.get("VIRGO_HOME")
        os.environ["VIRGO_HOME"] = str(home)
        try:
            ui = _CaptureUI()
            session = AgentSession(
                workspace,
                home=home,
                ui=ui,
                model="faux/test",
                session=None,
                extensions=False,
                refresh_models=False,
            )
            try:
                assert session.services["execution"].selected_mode_id == "no-limit"
                notice = "\n".join(ui.lines)
                assert "WSL2" in notice, notice
                assert "No-Limit" in notice, notice
                assert "Update Windows" in notice, notice
            finally:
                session.shutdown()

            ui2 = _CaptureUI()
            second = AgentSession(
                workspace,
                home=home,
                ui=ui2,
                model="faux/test",
                session=None,
                extensions=False,
                refresh_models=False,
            )
            try:
                assert not any("WSL2" in line for line in ui2.lines), ui2.lines
            finally:
                second.shutdown()

            try:
                precheck_windows_sandbox()
            except ExecutionUnavailable as exc:
                assert exc.reason == "windows_null_device_unavailable", exc.reason
                text = str(exc)
                assert "WSL2" in text and "No-Limit" in text and "Update Windows" in text, text
            else:
                raise AssertionError("Expected precheck_windows_sandbox() to fail closed on stock Windows Server")
        finally:
            if previous is None:
                os.environ.pop("VIRGO_HOME", None)
            else:
                os.environ["VIRGO_HOME"] = previous
    print("Verified real unsupported-Windows fallback (default no-limit, 3-option notice, and fail-closed precheck).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help=r"Check whether \Device\Null allows S-1-15-2-1")
    group.add_argument(
        "--verify-unsupported",
        action="store_true",
        help="Verify unsupported-Windows default No-Limit + notice + fail-closed precheck before CI prep",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Not running on native Windows; nothing to do.")
        return 0
    if args.check:
        ok = check_null_device()
        print(f"null_device_allows_appcontainer={ok}")
        return 0 if ok else 1
    if args.verify_unsupported:
        verify_unsupported_fallback()
        return 0
    prepare_null_device()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
