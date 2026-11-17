"""Unicode console events for cancellable Windows forms."""

from __future__ import annotations

import ctypes as ct


class KeyEvent(ct.Structure):
    _fields_ = [("down", ct.c_int32), ("repeat", ct.c_uint16), ("virtual_key", ct.c_uint16),
                ("scan", ct.c_uint16), ("char", ct.c_uint16), ("controls", ct.c_uint32)]


class Event(ct.Union):
    _fields_ = [("key", KeyEvent), ("reserved", ct.c_byte * 16)]


class InputRecord(ct.Structure):
    _fields_ = [("type", ct.c_uint16), ("event", Event)]


class WindowsConsoleInput:
    def __init__(self, fd: int):
        import msvcrt

        self.handle = msvcrt.get_osfhandle(fd)
        self.api = ct.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("GetConsoleMode", [ct.c_void_p, ct.POINTER(ct.c_uint32)], ct.c_int32),
            ("SetConsoleMode", [ct.c_void_p, ct.c_uint32], ct.c_int32),
            ("WaitForSingleObject", [ct.c_void_p, ct.c_uint32], ct.c_uint32),
            ("ReadConsoleInputW", [ct.c_void_p, ct.POINTER(InputRecord), ct.c_uint32,
                                   ct.POINTER(ct.c_uint32)], ct.c_int32),
            ("FlushConsoleInputBuffer", [ct.c_void_p], ct.c_int32),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.original = ct.c_uint32()
        self._check(self.api.GetConsoleMode(self.handle, ct.byref(self.original)))

    @staticmethod
    def _check(ok):
        if not ok:
            raise ct.WinError(ct.get_last_error())

    def __enter__(self):
        # Read key events directly, including Ctrl+C; restore normal line input
        # after the form. Unicode U+00E0 is text, never a function-key prefix.
        self._check(self.api.SetConsoleMode(self.handle, self.original.value & ~0x0207))
        return self

    def __exit__(self, *_):
        self._check(self.api.SetConsoleMode(self.handle, self.original.value))

    def read(self) -> str:
        ready = self.api.WaitForSingleObject(self.handle, 50)
        if ready == 0x102:  # WAIT_TIMEOUT
            return ""
        if ready != 0:
            raise ct.WinError(ct.get_last_error())
        record, count = InputRecord(), ct.c_uint32()
        self._check(self.api.ReadConsoleInputW(self.handle, ct.byref(record), 1, ct.byref(count)))
        key = record.event.key
        if count.value and record.type == 1 and key.char and (key.down or key.virtual_key == 0x12):
            # Alt+numpad text may arrive on the Alt key-up event.
            return chr(key.char) * max(1, key.repeat)
        return ""

    def flush(self) -> None:
        self._check(self.api.FlushConsoleInputBuffer(self.handle))
