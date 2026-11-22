"""Private SQLite connections for durable logs and runtime-local evidence."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

_DURABLE_BUSY_TIMEOUT_MS = 2000


def _enable_wal(connection: sqlite3.Connection) -> None:
    # Switching journal modes can return SQLITE_BUSY without invoking SQLite's busy handler.
    deadline = time.monotonic() + _DURABLE_BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL").fetchone()
            break
        except sqlite3.OperationalError as error:
            remaining = deadline - time.monotonic()
            if (error.sqlite_errorcode & 0xFF) != sqlite3.SQLITE_BUSY or remaining <= 0:
                raise
            time.sleep(min(0.025, remaining))
    connection.execute(f"PRAGMA busy_timeout={_DURABLE_BUSY_TIMEOUT_MS}")


def connect(path: Path, *, durable: bool) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Diagnostic database must be a regular private file") from None
    else:
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=0 if durable else 1, check_same_thread=False, isolation_level=None)
    try:
        if durable:
            _enable_wal(connection)
        else:
            connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA cache_size=-2048")
        connection.execute("PRAGMA synchronous=" + ("FULL" if durable else "OFF"))
        return connection
    except BaseException:
        connection.close()
        raise
