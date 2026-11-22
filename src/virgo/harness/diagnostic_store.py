"""Private SQLite connections for durable logs and runtime-local evidence."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path


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
    # The durable log is shared by processes that each commit with synchronous=FULL.
    connection = sqlite3.connect(path, timeout=2 if durable else 1, check_same_thread=False, isolation_level=None)
    try:
        connection.execute("PRAGMA cache_size=-2048")
        connection.execute("PRAGMA journal_mode=" + ("WAL" if durable else "MEMORY"))
        connection.execute("PRAGMA synchronous=" + ("FULL" if durable else "OFF"))
        return connection
    except BaseException:
        connection.close()
        raise
