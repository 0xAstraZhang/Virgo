"""Live provider model directories; discovery never generates a completion."""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .auth import authenticated_model
from .catalog_versions import CodexCatalogVersion, VersionCancelled, VersionError
from .models import base_url
from .types import Model

CATALOG_TIMEOUT = 12.0
MAX_BODY_BYTES = 8 * 1024 * 1024


class CatalogError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CatalogCancelled(CatalogError):
    pass


@dataclass(frozen=True)
class CatalogModel:
    id: str
    metadata: dict = field(default_factory=dict)
    released_at: float | None = None


def _anthropic_api_version() -> str:
    from anthropic import Anthropic

    # Read SDK metadata only; no inference, credentials lookup, or API request.
    with Anthropic(api_key="catalog-version-only") as client:
        return str(client.default_headers["anthropic-version"])


def _request(model: Model, codex_version: str | None = None) -> tuple[str, dict, dict]:
    """Use the same endpoint and credentials as inference, without forwarding redirects."""
    base = base_url(model)
    token = model.options.get("apiKey") or os.environ.get(model.api_key_env or "", "")
    headers = {**(model.options.get("headers") or {}), "Accept": "application/json"}
    params = {}
    if model.api == "google-gemini-cli":
        raise CatalogError("This account API has no supported live model directory; use /model provider/model-id.")
    if model.api == "openai-codex":
        account = model.options.get("authMetadata", {}).get("accountId")
        if not token or not account:
            raise CatalogError("Sign in again with /login auth openai.")
        base = (base or "https://chatgpt.com/backend-api/codex").rstrip("/")
        if base.endswith("/responses"):
            base = base.removesuffix("/responses")
        if not base.endswith("/codex"):
            base += "/codex"
        headers.update({"Authorization": f"Bearer {token}", "chatgpt-account-id": account, "originator": "virgo"})
        if codex_version is None:
            raise CatalogError("The Codex catalog client version has not been resolved.")
        params["client_version"] = codex_version
    elif model.api == "anthropic":
        base = (base or "https://api.anthropic.com").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        oauth = model.options.get("authType") == "oauth" or "sk-ant-oat" in token
        if model.provider == "anthropic" and not token:
            token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
            oauth = bool(token)
        if not token:
            raise CatalogError("No credentials for the model directory; use /login.")
        if oauth:
            from .providers.anthropic import BETA_OAUTH

            headers.update({"Authorization": f"Bearer {token}", "anthropic-beta": ",".join(BETA_OAUTH), "x-app": "cli"})
        else:
            headers["x-api-key"] = token
        headers["anthropic-version"] = str(model.options.get("apiVersion") or _anthropic_api_version())
        params["limit"] = 100
    elif model.api in {"openai", "openai-responses"}:
        if not base and model.provider != "openai":
            raise CatalogError("Configure this provider's API endpoint before listing models.")
        base = (base or "https://api.openai.com/v1").rstrip("/")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    else:
        raise CatalogError("This connection does not support a live model directory.")
    return base + "/models", headers, params


def _metadata(row: dict) -> dict:
    out = {}
    for dest, sources in {
        "context_window": ("context_window", "context_length", "max_input_tokens"),
        "max_tokens": ("max_output_tokens", "max_tokens"),
    }.items():
        value = next((row[key] for key in sources if row.get(key) is not None), None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out[dest] = value
    levels = row.get("supported_reasoning_levels", row.get("reasoning"))
    if isinstance(levels, list):
        values = [level.get("effort") if isinstance(level, dict) else level for level in levels]
        supported = tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value))
        if not levels or supported:
            out["supported_reasoning_levels"] = supported
            out["reasoning"] = bool(supported)
    elif isinstance(levels, bool):
        out["reasoning"] = levels
        if not levels:
            out["supported_reasoning_levels"] = ()
    default_level = row.get("default_reasoning_level")
    if isinstance(default_level, str) and default_level:
        out["default_reasoning_level"] = default_level
        out.setdefault("reasoning", True)
    capabilities = row.get("capabilities")
    if isinstance(capabilities, dict):
        thinking = capabilities.get("thinking")
        if isinstance(thinking, dict) and isinstance(thinking.get("supported"), bool):
            out["reasoning"] = thinking["supported"]
            if not thinking["supported"]:
                out["supported_reasoning_levels"] = ()
                out.pop("default_reasoning_level", None)
    return out


def _release_time(row: dict) -> float | None:
    for key in ("released_at", "release_date", "created_at", "created"):
        value = row.get(key)
        try:
            if isinstance(value, str):
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                timestamp = (date if date.tzinfo else date.replace(tzinfo=UTC)).timestamp()
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                timestamp = float(value)
            else:
                continue
            if math.isfinite(timestamp) and timestamp >= 0:
                return timestamp
        except (ValueError, OverflowError, OSError):
            continue
    return None


def _model(row: dict, api: str) -> CatalogModel | None:
    model_id = row.get("slug" if api == "openai-codex" else "id")
    if not isinstance(model_id, str) or not model_id or any(c.isspace() or ord(c) < 32 for c in model_id):
        return None
    if row.get("visibility", "list") != "list" or row.get("available") is False:
        return None
    status = row.get("lifecycle", row.get("status"))
    if isinstance(status, dict):
        status = status.get("status")
    if status in ("retired", "disabled", "unavailable"):
        return None
    modalities = row.get("output_modalities")
    if isinstance(modalities, list) and "text" not in modalities:
        return None
    # /models may also return embedding, media, moderation, and legacy completion models.
    if api in {"openai", "openai-responses"} and any(
        part in model_id.lower() for part in ("embedding", "whisper", "tts", "dall-e", "image", "realtime", "audio", "transcrib", "moderation", "sora", "babbage", "davinci")
    ):
        return None
    return CatalogModel(model_id, _metadata(row), _release_time(row))


def _read_catalog(model: Model, signal, stopped: threading.Event, version=None) -> list[CatalogModel]:
    url, headers, params = _request(model, version)
    results = {}
    cursors = set()
    with httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False) as client:
        for _ in range(50):
            if stopped.is_set() or (signal is not None and signal.is_set()):
                raise CatalogCancelled("Model discovery cancelled.")
            with client.stream("GET", url, headers=headers, params=params) as response:
                if response.status_code != 200:
                    # Never echo response bodies, URLs, or transport exceptions containing secrets.
                    raise CatalogError(f"Model directory returned HTTP {response.status_code}.", response.status_code)
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if stopped.is_set() or (signal is not None and signal.is_set()):
                        raise CatalogCancelled("Model discovery cancelled.")
                    payload.extend(chunk)
                    if len(payload) > MAX_BODY_BYTES:
                        raise CatalogError("Model directory response is too large.")
            try:
                body = json.loads(payload)
            except (ValueError, UnicodeError) as error:
                raise CatalogError("Model directory returned invalid JSON.") from error
            rows = body.get("models" if model.api == "openai-codex" else "data") if isinstance(body, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise CatalogError("Model directory returned an unsupported response.")
            for row in rows:
                if item := _model(row, model.api):
                    results.setdefault(item.id, item)
            if not body.get("has_more"):
                return list(results.values())
            cursor = body.get("last_id")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise CatalogError("Model directory returned an invalid pagination cursor.")
            cursors.add(cursor)
            params["after_id" if model.api == "anthropic" else "after"] = cursor
    raise CatalogError("Model directory has too many pages.")


def _fetch(model: Model, signal, stopped: threading.Event) -> list[CatalogModel]:
    model = authenticated_model(model, signal)
    if model.api != "openai-codex":
        return _read_catalog(model, signal, stopped)
    resolver = CodexCatalogVersion(model, signal, stopped)
    try:
        version = resolver.choose()
        try:
            models = _read_catalog(model, signal, stopped, version)
        except CatalogError as error:
            if resolver.override or error.status not in (400, 426):
                raise
            failure = str(error)
        else:
            if models:
                resolver.remember(version)
                return models
            if resolver.override:
                return []
            failure = "Codex returned no selectable models."
        # One forced release check and one alternate-version attempt, never an unbounded retry.
        try:
            version = resolver.choose(force=True, exclude={version})
        except VersionCancelled:
            raise
        except VersionError as error:
            raise CatalogError(f"{failure} Automatic version refresh found no alternative; check the connection or clientVersion setting.") from error
        models = _read_catalog(model, signal, stopped, version)
        if not models:
            raise CatalogError("Codex returned no selectable models after automatic version refresh; check the account and connection.")
        resolver.remember(version)
        return models
    except VersionCancelled as error:
        raise CatalogCancelled(str(error)) from error
    except VersionError as error:
        raise CatalogError(str(error)) from error


def fetch_model_catalog(model: Model, signal=None) -> list[CatalogModel]:
    """Bound the entire lookup and let Esc return promptly even if a provider stalls."""
    if signal is not None and signal.is_set():
        raise CatalogCancelled("Model discovery cancelled.")
    result = queue.Queue(maxsize=1)
    stopped = threading.Event()

    def fetch():
        try:
            result.put((_fetch(model, signal, stopped), None))
        except CatalogError as error:
            result.put((None, error))
        except Exception:
            result.put((None, CatalogError("Could not read the model directory. Check the connection and login, then retry /model.")))

    threading.Thread(target=fetch, name="virgo-model-catalog", daemon=True).start()
    deadline = time.monotonic() + CATALOG_TIMEOUT
    try:
        while True:
            if signal is not None and signal.is_set():
                raise CatalogCancelled("Model discovery cancelled.")
            if time.monotonic() >= deadline:
                raise CatalogError("Model directory request timed out. Retry /model.")
            try:
                models, error = result.get(timeout=0.05)
            except queue.Empty:
                continue
            if error:
                raise error
            return models
    finally:
        stopped.set()
