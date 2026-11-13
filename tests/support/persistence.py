"""Inject a one-file persistence fault while keeping the real session stack."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def fail_session_write(monkeypatch, path, *, partial=False, containing=""):
    original = builtins.open

    class FailedAppend:
        def __enter__(self):
            self.file = original(path, "a", encoding="utf-8")
            return self

        def write(self, text):
            if containing and containing not in text:
                return self.file.write(text)
            if partial:
                self.file.write(text[:10])
            raise OSError("session disk unavailable")

        def __exit__(self, *args):
            self.file.close()

    def open_file(target, *args, **kwargs):
        if Path(target) == path and (args[0] if args else kwargs.get("mode")) == "a":
            return FailedAppend()
        return original(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", open_file)
        yield
