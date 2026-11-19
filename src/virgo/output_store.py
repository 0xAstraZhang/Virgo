"""Broker-owned output records. Model-facing references are opaque capabilities."""
from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class OutputRecord:
    id: str
    store: OutputStore = field(repr=False, compare=False)
    _path: Path = field(repr=False, compare=False)

    def __str__(self):
        return self.id

    def unlink(self, *, missing_ok=False):
        self._path.unlink(missing_ok=missing_ok)
        self.store._records.pop(self.id, None)


class OutputPipe:
    """Expose only a pipe to children; only the Broker holds the spool file."""

    def __init__(self, destination):
        self._input, self._output = os.pipe()
        self._destination = destination
        self._error = None
        self.closed = False
        self._reader = threading.Thread(target=self._drain, name="virgo-output", daemon=True)
        self._reader.start()

    def _drain(self):
        with os.fdopen(self._input, "rb", buffering=0) as source, self._destination as target:
            while chunk := source.read(64 * 1024):
                if self._error is None:
                    try:
                        target.write(chunk)
                    except OSError as error:
                        # Continue draining so the child cannot block forever
                        # after a disk error. Report the failed capture at close.
                        self._error = error

    def fileno(self):
        return self._output

    def write(self, data):
        pending = memoryview(data)
        count = len(pending)
        while pending:
            pending = pending[os.write(self._output, pending):]
        return count

    def close(self):
        if not self.closed:
            os.close(self._output)
            self.closed = True
        self._reader.join(timeout=10)
        if self._reader.is_alive():
            raise RuntimeError("Output capture did not close after process cleanup")
        if self._error is not None:
            raise self._error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OutputStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._records: dict[str, OutputRecord] = {}

    def create(self, prefix="output"):
        # prefix is descriptive only and never participates in a filesystem path.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        identifier = "out_" + secrets.token_hex(24)
        path = self.directory / (identifier + ".log")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        record = OutputRecord(identifier, self, path)
        self._records[identifier] = record
        try:
            return record, OutputPipe(os.fdopen(descriptor, "wb", buffering=0))
        except BaseException:
            record.unlink(missing_ok=True)
            raise

    def lookup(self, identifier):
        from .execution import FileAccessDenied

        record = self._records.get(identifier)
        if record is None:
            raise FileAccessDenied("Output ID is unknown or belongs to another task", dimension="output_id")
        return record

    def read(self, record, *, offset=0, limit=50 * 1024, tail=False):
        if not isinstance(record, OutputRecord) or self.lookup(record.id) is not record:
            from .execution import FileAccessDenied

            raise FileAccessDenied("Output record does not belong to this task", dimension="output_id")
        with record._path.open("rb") as stream:
            total = stream.seek(0, os.SEEK_END)
            start = max(0, total - limit) if tail else min(max(0, offset), total)
            stream.seek(start)
            data = stream.read(limit)
        return data, total, start


def private_output_store():
    user = Path(os.environ.get("VIRGO_HOME") or Path.home() / ".virgo")
    return OutputStore(user / "outputs" / secrets.token_hex(16))
