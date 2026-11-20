"""User-owned, transactional resource grants for exact tool invocations."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

LEGACY_GRANTS_FILENAME = "resource-grants.sqlite3"
GRANTS_FILENAME = "permission-grants.sqlite3"


def operation_id(event):
    shell = getattr(event.tool, "shell", None)
    fingerprint = getattr(event.tool, "config_fingerprint", None)
    data = {
        "tool": event.tool_name, "arguments": event.args,
        "shell": [shell.kind, shell.executable, shell.version] if shell else None,
    }
    if fingerprint is not None:
        data["config_fingerprint"] = fingerprint
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def external_operation_id(event) -> str:
    """Bind persistent 'Always allow' for an MCP/external tool to its server config fingerprint (S4-02)."""
    return json.dumps({
        "tool": event.tool_name,
        "config_fingerprint": getattr(event.tool, "config_fingerprint", None),
    }, sort_keys=True, ensure_ascii=False)


class ResourceRules:
    def __init__(self, user_directory: Path):
        self.path = Path(user_directory) / GRANTS_FILENAME

    @staticmethod
    def purge_legacy(user_directory: Path) -> bool:
        """Delete the retired pre-Policy saved-grants database without migration (S3-06)."""
        base = Path(user_directory) / LEGACY_GRANTS_FILENAME
        removed = False
        for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-shm"), Path(f"{base}-journal")):
            try:
                candidate.unlink()
                if candidate == base:
                    removed = True
            except FileNotFoundError:
                pass
        return removed

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
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, mode TEXT NOT NULL,
                    cwd TEXT NOT NULL, operation TEXT NOT NULL, resource TEXT NOT NULL,
                    target TEXT NOT NULL, directory INTEGER NOT NULL,
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
        matched = []
        for kind, target, directory in rows:
            if kind == "network_allowed":
                if target == "true":
                    matched.append((kind, "true"))
                continue
            if kind == "external_tool":
                if target:
                    matched.append((kind, target))
                continue
            path = Path(target)
            if path.resolve() == path and path.is_dir() == bool(directory):
                matched.append((kind, target))
        return matched

    def allow(self, scope, resources):
        with self.database(create=True) as db:
            db.executemany("""INSERT INTO grants
                (project, mode, cwd, operation, resource, target, directory) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(project, mode, cwd, operation, resource, target)
                DO UPDATE SET directory=excluded.directory""", (
                (*scope, kind, "true" if kind == "network_allowed" else target,
                 int(kind not in {"network_allowed", "external_tool"} and Path(target).is_dir()))
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
