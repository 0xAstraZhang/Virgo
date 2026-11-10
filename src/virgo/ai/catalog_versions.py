"""Discover Codex catalog client versions without installing or updating software."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from .auth import AuthStore
from .types import Model

CODEX_RELEASE_URL = "https://registry.npmjs.org/@openai/codex/latest"
VERSION_TTL = 24 * 60 * 60
_CACHE_LOCK = threading.Lock()
_STABLE_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


class VersionError(Exception):
    pass


class VersionCancelled(VersionError):
    pass


def valid_version(value) -> str | None:
    return value if isinstance(value, str) and len(value) <= 40 and _STABLE_VERSION.fullmatch(value) else None


def installed_codex_version() -> str | None:
    executable = shutil.which("codex")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=1.5, check=True,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(r"codex(?:-cli)?\s+v?(\S+)\s*", result.stdout)
    return valid_version(match[1]) if match else None


class CodexCatalogVersion:
    def __init__(self, model: Model, signal=None, stopped=None):
        self.override = model.options.get("clientVersion")
        self.path = AuthStore(model.auth_path).path.parent / "cache" / "codex-catalog-version.json"
        self.signal = signal
        self.stopped = stopped
        self.local: str | None = None
        self._local_checked = False
        self.cache = self._read()

    def _check_cancel(self) -> None:
        if any(event is not None and event.is_set() for event in (self.signal, self.stopped)):
            raise VersionCancelled("Model discovery cancelled.")

    def _read(self) -> dict:
        try:
            if self.path.stat().st_size > 4096:
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
        except (OSError, ValueError):
            return {}
        out = {key: value for key in ("published", "verified") if (value := valid_version(data.get(key)))}
        checked = data.get("checked_at")
        if isinstance(checked, (int, float)) and not isinstance(checked, bool) and 0 < checked <= time.time():
            out["checked_at"] = checked
        return out

    def _save(self, patch: dict) -> None:
        self._check_cancel()
        self.cache.update(patch)
        temp = None
        try:
            with _CACHE_LOCK:
                data = {**self._read(), **self.cache}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix=".codex-version-", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                    handle.write("\n")
                os.replace(temp, self.path)
        except OSError:
            pass  # Discovery also works with a read-only home or cache.
        finally:
            if temp is not None:
                try:
                    Path(temp).unlink(missing_ok=True)
                except OSError:
                    pass

    def _published_version(self) -> str:
        self._check_cancel()
        # A separate, unauthenticated client: never send provider credentials to the registry.
        with httpx.Client(timeout=2.0, follow_redirects=False) as client:
            with client.stream("GET", CODEX_RELEASE_URL, headers={"Accept": "application/json"}) as response:
                if response.status_code != 200:
                    raise VersionError("Could not read the official Codex release version.")
                body = bytearray()
                for chunk in response.iter_bytes():
                    self._check_cancel()
                    body.extend(chunk)
                    if len(body) > 256 * 1024:
                        raise VersionError("Codex release metadata is too large.")
        data = json.loads(body)
        version = valid_version(data.get("version")) if isinstance(data, dict) else None
        if not version or data.get("name") != "@openai/codex":
            raise VersionError("The official Codex release metadata has no stable version.")
        return version

    def choose(self, *, force=False, exclude: set[str] | None = None) -> str:
        self._check_cancel()
        if self.override:
            if version := valid_version(self.override):
                return version
            raise VersionError("providers.openai-codex.clientVersion must be a stable version such as 1.2.3.")
        if not self._local_checked:
            self.local = installed_codex_version()
            self._local_checked = True
        if force or time.time() - self.cache.get("checked_at", 0) >= VERSION_TTL:
            try:
                published = self._published_version()
            except VersionCancelled:
                raise
            except (VersionError, httpx.HTTPError, ValueError):
                pass  # Use installed or previously discovered versions when metadata is unavailable.
            else:
                self._save({"published": published, "checked_at": time.time()})
        self._check_cancel()
        candidates = {
            version for value in (self.local, self.cache.get("published"), self.cache.get("verified"))
            if (version := valid_version(value)) and version not in (exclude or set())
        }
        if not candidates:
            raise VersionError(
                "No Codex catalog version is available. Check the network or set providers.openai-codex.clientVersion."
            )
        return max(candidates, key=lambda value: tuple(map(int, value.split("."))))

    def remember(self, version: str) -> None:
        if not self.override and valid_version(version):
            self._save({"verified": version})
