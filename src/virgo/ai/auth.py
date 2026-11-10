"""User-scoped credentials, refreshed at request time (inspired by Pi's AuthStorage)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .types import Model

try:
    import fcntl
except ImportError:  # Windows still serializes in-process callers.
    fcntl = None


class AuthStore:
    _lock = threading.RLock()

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(os.environ.get("VIRGO_HOME", Path.home() / ".virgo")) / "auth.json"

    def all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"Invalid credentials file: {self.path}") from exc
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValueError(f"Invalid credentials file: {self.path}")
        return data

    @contextmanager
    def locked(self):
        with self._lock:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "a") as handle:
                if fcntl:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl:
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def _write(self, data: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def set(self, provider: str, credential: dict) -> None:
        with self.locked():
            data = self.all()
            data[provider] = copy.deepcopy(credential)
            self._write(data)

    def save_login(self, provider: str, credential: dict, save_settings) -> None:
        """Roll back credentials if the accompanying connection settings cannot be saved."""
        with self.locked():
            previous = self.all()
            data = {**previous, provider: copy.deepcopy(credential)}
            self._write(data)
            try:
                save_settings()
            except Exception:
                self._write(previous)
                raise

    def remove(self, providers: list[str]) -> list[str]:
        with self.locked():
            data = self.all()
            removed = [p for p in providers if p in data]
            for provider in removed:
                del data[provider]
            if removed:
                self._write(data)
            return removed

    def credential(self, provider: str, signal=None) -> dict | None:
        credential = self.all().get(provider)
        if not credential or credential.get("type") != "oauth":
            return credential
        if float(credential.get("expires", 0)) > time.time() + 60:
            return credential
        # Re-read under the shared file lock: another process may have refreshed
        # the rotating token, or logged out, while we were waiting.
        with self.locked():
            data = self.all()
            credential = data.get(provider)
            if not credential or credential.get("type") != "oauth":
                return credential
            if float(credential.get("expires", 0)) <= time.time() + 60:
                from .oauth import refresh

                updated = refresh(provider, copy.deepcopy(credential), signal=signal)
                credential = {**credential, **updated}
                if signal is not None and signal.is_set():
                    raise ValueError("Authentication cancelled")
                data[provider] = credential
                self._write(data)
            return credential


def authenticated_model(model: Model, signal=None) -> Model:
    """Return a request-only copy; long-lived models and session files contain no tokens."""
    if model.api == "faux":
        return model
    store = AuthStore(model.auth_path)
    credential = store.credential(model.provider, signal)
    if not credential:
        return model
    kind = credential.get("type")
    key = credential.get("access" if kind == "oauth" else "key")
    if kind not in ("oauth", "api_key") or not isinstance(key, str) or not key:
        raise ValueError(f"Invalid credential for {model.provider}; use /login again")
    connection = credential.get("connection")
    if connection is not None:
        base = model.base_url or ""
        env_name = model.options.get("baseUrlEnv")
        if env_name and not base:
            base = os.environ.get(env_name) or ""
        if model.api != connection["api"] or base.rstrip("/") != connection["baseUrl"].rstrip("/"):
            raise ValueError(f"The endpoint for {model.provider} changed; use /login again for this endpoint")
    if kind == "oauth":
        from .auth_presets import AUTH_PRESETS

        preset = AUTH_PRESETS.get(model.provider)
        if preset is None:
            raise ValueError(f"Unsupported account provider: {model.provider}")
        expected = preset["config"].get("baseUrl")
        configured = model.base_url or expected
        env_name = model.options.get("baseUrlEnv")
        if env_name and not model.base_url:
            configured = os.environ.get(env_name) or configured
        if configured and expected and configured.rstrip("/") != expected.rstrip("/"):
            raise ValueError(f"Account login for {model.provider} requires its official endpoint")
    options = dict(model.options)
    options.update(apiKey=key, authType=kind)
    options["authMetadata"] = {
        k: v for k, v in credential.items()
        if k not in ("access", "refresh", "key", "type", "expires", "config", "model", "headers", "connection")
    }
    options["headers"] = {**options.get("headers", {}), **credential.get("headers", {})}
    return replace(model, options=options)
