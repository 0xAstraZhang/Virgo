"""User-owned, transactional resource grants scoped by project, mode, and resource prefix."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

LEGACY_GRANTS_FILENAME = "resource-grants.sqlite3"
GRANTS_FILENAME = "permission-grants.sqlite3"


def external_tool_target(event) -> str | None:
    """Bind persistent 'Always allow' for an MCP/external tool to its server config fingerprint."""
    fingerprint = getattr(event.tool, "config_fingerprint", None)
    if not fingerprint:
        return None
    return f"{event.tool_name}:{fingerprint}"


class ResourceRules:
    def __init__(self, user_directory: Path):
        self.path = Path(user_directory) / GRANTS_FILENAME

    @staticmethod
    def purge_legacy(user_directory: Path) -> bool:
        """Delete the retired pre-Policy saved-grants database without migration."""
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
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        uri = self.path.resolve().as_uri() + ("?mode=rw" if create or write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as db, db:
            if create or write:
                db.execute("DROP TABLE IF EXISTS grants")
            if create:
                db.execute("""CREATE TABLE IF NOT EXISTS resource_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    target TEXT NOT NULL,
                    UNIQUE(project, mode, resource, target)
                )""")
            elif db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resource_grants'"
            ).fetchone() is None:
                yield None
                return
            yield db

    def match(self, project: str, mode: str) -> list[tuple[str, str]]:
        with self.database() as db:
            if db is None:
                return []
            rows = db.execute(
                """SELECT resource, target FROM resource_grants
                   WHERE project=? AND mode=? ORDER BY id""",
                (project, mode),
            ).fetchall()
        matched: list[tuple[str, str]] = []
        for kind, target in rows:
            if kind == "network_allowed":
                if target == "true":
                    matched.append((kind, "true"))
                continue
            if kind == "external_tool":
                if target:
                    matched.append((kind, target))
                continue
            if kind in {"read_paths", "write_paths"}:
                path = Path(target)
                if not path.is_absolute():
                    continue
                if path.exists():
                    try:
                        if path.resolve() != path:
                            continue
                    except OSError:
                        continue
                matched.append((kind, target))
        return matched

    def allow(self, project: str, mode: str, resources) -> None:
        with self.database(create=True) as db:
            db.executemany(
                """INSERT OR IGNORE INTO resource_grants (project, mode, resource, target)
                   VALUES (?, ?, ?, ?)""",
                (
                    (project, mode, kind, "true" if kind == "network_allowed" else str(target))
                    for kind, target in resources
                ),
            )

    def list(self, project: str):
        with self.database() as db:
            if db is None:
                return []
            return db.execute(
                """SELECT id, mode, resource, target FROM resource_grants
                   WHERE project=? ORDER BY id""",
                (project,),
            ).fetchall()

    def revoke(self, project: str, identifier: int) -> bool:
        with self.database(write=True) as db:
            return bool(
                db
                and db.execute(
                    "DELETE FROM resource_grants WHERE project=? AND id=?",
                    (project, identifier),
                ).rowcount
            )
