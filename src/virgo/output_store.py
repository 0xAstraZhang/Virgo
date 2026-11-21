"""Broker-owned output records with per-session persistence and UTF-8-aligned paging.

Model-facing references are opaque output IDs (never host filesystem paths).
Children write through an OS pipe; only the Broker holds the backing file.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_SECONDS = 7 * 24 * 3600
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]")
_REGISTRY_FILE = "registry.json"


def _harden_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _trim_incomplete_utf8_tail(data: bytes) -> bytes:
    """Drop an incomplete trailing UTF-8 multibyte sequence when splitting mid-file."""
    if not data:
        return data
    # Walk back up to 3 bytes looking for a multibyte lead byte (0b11xxxxxx).
    length = len(data)
    for back in range(1, min(4, length) + 1):
        byte = data[length - back]
        if byte < 0x80:
            return data  # ASCII ends cleanly.
        if (byte & 0xC0) != 0x80:
            # Found a UTF-8 lead byte at index `length - back`.
            if (byte & 0xE0) == 0xC0:
                expected = 2
            elif (byte & 0xF0) == 0xE0:
                expected = 3
            elif (byte & 0xF8) == 0xF0:
                expected = 4
            else:
                return data  # Invalid byte; leave for replacement decoding.
            if back < expected:
                trimmed = data[: length - back]
                return trimmed if trimmed else data
            return data
    return data


@dataclass(frozen=True)
class OutputRecord:
    id: str
    store: OutputStore = field(repr=False, compare=False)
    _path: Path = field(repr=False, compare=False)

    def __str__(self) -> str:
        return self.id

    def unlink(self, *, missing_ok: bool = False) -> None:
        self._path.unlink(missing_ok=missing_ok)
        self.store._remove_record(self.id)


class OutputPipe:
    """Expose only a pipe to children; only the Broker holds the spool file."""

    def __init__(self, destination):
        self._input, self._output = os.pipe()
        self._destination = destination
        self._error: OSError | None = None
        self.closed = False
        self._reader = threading.Thread(target=self._drain, name="virgo-output", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        with os.fdopen(self._input, "rb", buffering=0) as source, self._destination as target:
            while chunk := source.read(64 * 1024):
                if self._error is None:
                    try:
                        pending = memoryview(chunk)
                        while pending:
                            written = target.write(pending)
                            if not written:
                                raise OSError("Output capture target stopped accepting bytes")
                            pending = pending[written:]
                    except OSError as error:
                        # Continue draining so the child cannot block forever
                        # after a disk error. Report the failed capture at close.
                        self._error = error

    def fileno(self) -> int:
        return self._output

    def write(self, data: bytes) -> int:
        pending = memoryview(data)
        count = len(pending)
        while pending:
            pending = pending[os.write(self._output, pending):]
        return count

    def close(self) -> None:
        if not self.closed:
            os.close(self._output)
            self.closed = True
        self._reader.join(timeout=10)
        if self._reader.is_alive():
            raise RuntimeError("Output capture did not close after process cleanup")
        if self._error is not None:
            raise self._error

    def __enter__(self) -> OutputPipe:
        return self

    def __exit__(self, *_) -> None:
        self.close()


class OutputStore:
    """Session-scoped private output store with persistent registry."""

    def __init__(self, directory: Path, *, persist: bool = True):
        self.directory = Path(directory)
        self._persist = persist
        self._lock = threading.RLock()
        self._records: dict[str, OutputRecord] = {}
        if self._persist:
            self._load_registry()

    def _registry_path(self) -> Path:
        return self.directory / _REGISTRY_FILE

    def _load_registry(self) -> None:
        reg = self._registry_path()
        try:
            raw = json.loads(reg.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        for identifier in raw:
            if not isinstance(identifier, str) or not re.fullmatch(r"out_[0-9a-f]{8,48}", identifier):
                continue
            path = self.directory / f"{identifier}.log"
            if path.is_file() and not path.is_symlink():
                self._records.setdefault(identifier, OutputRecord(identifier, self, path))

    def _save_registry(self) -> None:
        if not self._persist or not self.directory.is_dir():
            return
        payload = json.dumps(sorted(self._records), ensure_ascii=True).encode("ascii")
        fd, tmp_name = tempfile.mkstemp(prefix=".registry-", suffix=".tmp", dir=self.directory)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            if os.name == "posix":
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            os.replace(tmp, self._registry_path())
            tmp = None
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _remove_record(self, identifier: str) -> None:
        with self._lock:
            removed = self._records.pop(identifier, None) is not None
            if removed and self._persist:
                try:
                    self._save_registry()
                except OSError:
                    pass

    def create(self, prefix: str = "output") -> tuple[OutputRecord, OutputPipe]:
        # prefix is descriptive only and never participates in a filesystem path.
        _harden_dir(self.directory.parent)
        _harden_dir(self.directory)
        identifier = "out_" + secrets.token_hex(6)
        path = self.directory / (identifier + ".log")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            0o600,
        )
        record = OutputRecord(identifier, self, path)
        with self._lock:
            self._records[identifier] = record
            if self._persist:
                self._save_registry()
        try:
            return record, OutputPipe(os.fdopen(descriptor, "wb", buffering=0))
        except BaseException:
            record.unlink(missing_ok=True)
            raise

    def lookup(self, identifier: str) -> OutputRecord:
        from .execution import FileAccessDenied

        with self._lock:
            record = self._records.get(identifier)
            if record is None and self._persist:
                self._load_registry()
                record = self._records.get(identifier)
        if record is None or record._path.is_symlink() or not record._path.is_file():
            raise FileAccessDenied("Output ID is unknown or belongs to another session", dimension="output_id")
        return record

    def read(self, record: OutputRecord, *, offset: int = 0, limit: int = 50 * 1024,
             tail: bool = False, align_utf8: bool = False) -> tuple[bytes, int, int]:
        from .execution import FileAccessDenied

        if not isinstance(record, OutputRecord) or self.lookup(record.id) is not record:
            raise FileAccessDenied("Output record does not belong to this session", dimension="output_id")
        try:
            if record._path.is_symlink() or not stat.S_ISREG(os.lstat(record._path).st_mode):
                raise FileAccessDenied("Output record cannot be opened safely: symlink", dimension="output_id")
        except OSError as error:
            raise FileAccessDenied(f"Output record cannot be opened safely: {error}", dimension="output_id") from error
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(record._path, flags)
        except OSError as error:
            raise FileAccessDenied(f"Output record cannot be opened safely: {error}", dimension="output_id") from error
        with os.fdopen(fd, "rb") as stream:
            total = stream.seek(0, os.SEEK_END)
            if tail:
                start = max(0, total - limit)
                stream.seek(start)
                data = stream.read(limit)
                return data, total, start
            start = min(max(0, offset), total)
            stream.seek(start)
            if align_utf8 and start > 0 and start < total:
                # Skip up to 3 UTF-8 continuation bytes so paging starts on a codepoint boundary.
                peek = stream.read(3)
                skip = 0
                while skip < len(peek) and (peek[skip] & 0xC0) == 0x80:
                    skip += 1
                start += skip
                stream.seek(start)
            data = stream.read(limit)
            if align_utf8 and start + len(data) < total:
                trimmed = _trim_incomplete_utf8_tail(data)
                if trimmed:
                    data = trimmed
                elif limit < 4:
                    # Ensure forward progress if caller asked for fewer than 4 bytes on a multibyte char.
                    extra = stream.read(4 - len(data))
                    for extra_len in range(1, len(extra) + 1):
                        candidate = data + extra[:extra_len]
                        try:
                            candidate.decode("utf-8")
                            data = candidate
                            break
                        except UnicodeDecodeError:
                            continue
        return data, total, start

    def delete_all(self) -> None:
        with self._lock:
            self._records.clear()
        shutil.rmtree(self.directory, ignore_errors=True)

    @staticmethod
    def session_directory(user_dir: Path, session_id: str) -> Path:
        safe = _SAFE_SESSION.sub("_", str(session_id or "ephemeral"))[:128] or "ephemeral"
        return Path(user_dir) / "outputs" / f"session-{safe}"

    @classmethod
    def for_session(cls, user_dir: Path, session_id: str, *, persist: bool = True) -> OutputStore:
        return cls(cls.session_directory(user_dir, session_id), persist=persist)

    @classmethod
    def delete_session(cls, user_dir: Path, session_id: str) -> None:
        shutil.rmtree(cls.session_directory(user_dir, session_id), ignore_errors=True)

    @staticmethod
    def prune_expired(outputs_root: Path, *, max_age_seconds: float = DEFAULT_TTL_SECONDS) -> int:
        """Delete expired output directories older than ``max_age_seconds``."""
        root = Path(outputs_root)
        if not root.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed


def private_output_store() -> OutputStore:
    user = Path(os.environ.get("VIRGO_HOME") or Path.home() / ".virgo")
    return OutputStore(user / "outputs" / ("ephemeral-" + secrets.token_hex(12)), persist=False)
