"""Private SQLite connections for durable logs and runtime-local evidence."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

_BUSY_TIMEOUT_SECONDS = 1


def execute_durable_write(connection: sqlite3.Connection, sql: str, parameters=()) -> None:
    # Use one deadline for the statement. WAL negotiation may skip SQLite's busy
    # handler, and a statement can encounter contention at more than one lock.
    deadline = time.monotonic() + 2
    connection.execute("PRAGMA busy_timeout=0")
    try:
        while True:
            try:
                connection.execute(sql, parameters).close()
                return
            except sqlite3.OperationalError as error:
                remaining = deadline - time.monotonic()
                if (error.sqlite_errorcode & 0xFF) != sqlite3.SQLITE_BUSY or remaining <= 0:
                    raise
                time.sleep(min(0.025, remaining))
    finally:
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_SECONDS * 1000}")


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
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS, check_same_thread=False, isolation_level=None)
    try:
        if durable:
            execute_durable_write(connection, "PRAGMA journal_mode=WAL")
        else:
            connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA cache_size=-2048")
        connection.execute("PRAGMA synchronous=" + ("FULL" if durable else "OFF"))
        return connection
    except BaseException:
        connection.close()
        raise
