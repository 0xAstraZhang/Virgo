"""User-owned, transactional resource grants for exact tool invocations."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


def operation_id(event):
    shell = getattr(event.tool, "shell", None)
    return json.dumps({
        "tool": event.tool_name, "arguments": event.args,
        "shell": [shell.kind, shell.executable, shell.version] if shell else None,
    }, sort_keys=True, ensure_ascii=False)


class ResourceRules:
    def __init__(self, user_directory):
        self.path = user_directory / "resource-grants.sqlite3"

    @contextmanager
    def database(self, *, create=False, write=False):
        if not create and not self.path.exists():
            yield None
            return
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # SQLite derives journal permissions from the database. Exact command
            # arguments can contain private values even for a read-only grant.
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        uri = self.path.resolve().as_uri() + ("?mode=rw" if create or write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db, db:
            if create:
                db.execute("""CREATE TABLE IF NOT EXISTS grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, mode TEXT, cwd TEXT,
                    operation TEXT, resource TEXT, target TEXT, directory INTEGER,
                    UNIQUE(project, mode, cwd, operation, resource, target)
                )""")
            elif db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='grants'").fetchone() is None:
                # A concurrent first approval has opened the file but has not committed yet.
                yield None
                return
            yield db

    def match(self, scope):
        with self.database() as db:
            if db is None:
                return []
            rows = db.execute("""SELECT resource, target, directory FROM grants
                WHERE project=? AND mode=? AND cwd=? AND operation=?""", scope).fetchall()
        # A file grant cannot acquire directory semantics on a later invocation.
        return [(kind, target) for kind, target, directory in rows if kind == "network_allowed"
                or (Path(target).resolve() == Path(target) and Path(target).is_dir() == bool(directory))]

    def allow(self, scope, resources):
        with self.database(create=True) as db:
            db.executemany("""INSERT INTO grants
                (project, mode, cwd, operation, resource, target, directory) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(project, mode, cwd, operation, resource, target)
                DO UPDATE SET directory=excluded.directory""", (
                (*scope, kind, target, kind != "network_allowed" and Path(target).is_dir())
                for kind, target in resources
            ))

    def list(self, project):
        with self.database() as db:
            if db is None:
                return []
            return db.execute("""SELECT id, mode, cwd, operation, resource, target FROM grants
                WHERE project=? ORDER BY id""", (project,)).fetchall()

    def revoke(self, project, identifier):
        with self.database(write=True) as db:
            return bool(db and db.execute("DELETE FROM grants WHERE project=? AND id=?",
                                          (project, identifier)).rowcount)
