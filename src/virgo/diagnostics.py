"""Portable, bounded error records shared by AI, agent and harness layers.

Capturing a failure does no file I/O. Records contain stack locations, never frame
locals or source lines, and deliberately omit request bodies and credentials.
Persistence belongs to the harness; even its emergency reporter must not raise.
"""

from __future__ import annotations

import math
import os
import re
import sys
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

_REDACTED = "[redacted]"
_SECRET_KEY = re.compile(
    r"authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|"
    r"client[-_]?secret|password|passwd|credential|cookie|(?:^|[_-])secret(?:$|[_-])|^token$", re.I,
)
_SECRET_ENV = re.compile(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|SAS)(?:$|_)", re.I)
_PAYLOAD_KEYS = {"body", "headers", "prompt", "messages", "arguments", "args", "input", "output", "content"}
_SCHEMA_KEYS = {
    "schema_version", "error_id", "timestamp", "code", "phase", "kind", "message", "exception_type",
    "traceback", "cause_chain", "context", "relation", "frames", "file", "line", "function",
    "omitted_frames", "exceptions", "chain_truncated", "log_path", "log_pattern", "log_expired",
    "logging_error", "reporting_error", "record_warning", "record_truncated", "original_bytes",
}
_ASSIGNMENT = re.compile(
    r'''(?ix)(["']?(?:authorization|proxy-authorization|x-api-key|api[_-]?key|access[_-]?token|'''
    r'''refresh[_-]?token|id[_-]?token|client[_-]?secret|password|passwd|cookie|secret|token)["']?\s*[:=]\s*)'''
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)''',
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_COOKIE_HEADER = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)(?![\"'])[^\r\n]*")
_MAX_TEXT = 8192
_MAX_FRAMES = 64
_MAX_CHAIN = 8
_MAX_NODES = 4096
_MAX_CHARACTERS = 256_000


def _text(value) -> str:
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}: text unavailable>"


def _secrets(values: Iterable[str]) -> tuple[str, ...]:
    # Explicit adapter secrets may be short. Environment values have a minimum
    # length to avoid replacing ordinary words such as "1" everywhere.
    found = []
    try:
        for value in values:
            if isinstance(value, str) and value:
                found.append(_text(value))
    except Exception:
        pass  # A malformed optional secret list must not erase the original failure.
    for key in list(os.environ):
        value = os.environ.get(key)
        if isinstance(value, str) and len(value) >= 4 and _SECRET_ENV.search(key):
            found.append(value)
    return tuple(sorted(set(found), key=len, reverse=True))


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if len(secret) < 4:
            # Short values also occur as letters/digits inside ordinary words
            # and UUIDs. Hide an independent short token without corrupting them.
            text = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", _REDACTED, text)
        else:
            text = text.replace(secret, _REDACTED)
        # POSIX environment values may contain surrogate-escaped bytes.
        encoded = quote(secret, safe="", errors="surrogatepass")
        if encoded != secret:
            text = text.replace(encoded, _REDACTED)

    def clean_url(match):
        raw = match.group()
        try:
            parts = urlsplit(raw)
            host = parts.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parts.scheme, host, parts.path, _REDACTED if parts.query else "", ""))
        except ValueError:
            return "[invalid URL omitted]"

    text = _URL.sub(clean_url, text)
    # Unquoted HTTP cookie headers can contain many credentials separated by
    # semicolons. A generic assignment matcher would redact only the first one.
    text = _COOKIE_HEADER.sub(lambda match: match[1] + _REDACTED, text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", text)
    text = _ASSIGNMENT.sub(lambda match: match[1] + _REDACTED, text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b",
                  _REDACTED, text)
    # Error text is also rendered in terminals: no terminal controls or ANSI escapes.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    clean = "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)
    return clean.encode("utf-8", errors="backslashreplace").decode("utf-8")


def safe_text(text, *, secrets: Iterable[str] = ()) -> str:
    """Redact known secrets, auth assignments and URL credentials/query strings."""
    try:
        clean = _redact(_text(text), _secrets(secrets))
        return clean if len(clean) <= _MAX_TEXT else clean[:_MAX_TEXT] + "\n[truncated]"
    except Exception:
        # Do not return the unredacted input when the redactor itself failed.
        return "[text unavailable: diagnostic redaction failed]"


def sanitize(value, *, secrets: Iterable[str] = ()):
    """Copy diagnostic metadata to bounded JSON values, without arbitrary repr()."""
    sensitive = _secrets(secrets)
    active: set[int] = set()
    remaining_nodes = _MAX_NODES
    remaining_characters = _MAX_CHARACTERS

    def walk(item, depth=0):
        nonlocal remaining_nodes, remaining_characters
        if remaining_nodes <= 0 or remaining_characters <= 0:
            return "[diagnostic size limit]"
        remaining_nodes -= 1
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            # json.dumps rejects integers with enormous decimal expansions.
            return int.__int__(item) if int.bit_length(item) <= 2048 else "[integer too large]"
        if isinstance(item, float):
            return float.__float__(item) if math.isfinite(item) else _text(item)
        if isinstance(item, (str, Path)):
            text = _redact(_text(item), sensitive)
            limit = min(32_768 if depth == 1 else _MAX_TEXT, remaining_characters)
            remaining_characters -= min(len(text), limit)
            return text if len(text) <= limit else text[:limit] + "\n[truncated]"
        if depth >= 8:
            return "[maximum diagnostic depth]"
        if id(item) in active:
            return "[cycle omitted]"
        if isinstance(item, (Mapping, list, tuple)):
            active.add(id(item))
            try:
                if isinstance(item, Mapping):
                    result = {}
                    for index, (key, content) in enumerate(item.items()):
                        if index >= 64 or remaining_nodes <= 0 or remaining_characters <= 0:
                            result["_truncated"] = "additional fields omitted"
                            break
                        raw_key = _text(key)
                        key = raw_key if raw_key in _SCHEMA_KEYS else _redact(raw_key, sensitive)[:256]
                        result[key] = (
                            _REDACTED if _SECRET_KEY.search(raw_key) or raw_key.lower() in _PAYLOAD_KEYS
                            else visit(content, depth + 1)
                        )
                    return result
                result = []
                for index, child in enumerate(item):
                    if index >= 64 or remaining_nodes <= 0 or remaining_characters <= 0:
                        result.append("[additional items omitted]")
                        break
                    result.append(visit(child, depth + 1))
                return result
            except Exception:
                # Custom Mapping implementations may raise from items/iteration.
                return "[diagnostic metadata unavailable]"
            finally:
                active.discard(id(item))
        return f"<{type(item).__name__}>"

    def visit(item, depth=0):
        try:
            return walk(item, depth)
        except Exception:
            return "[diagnostic metadata unavailable]"

    return visit(value)


def _exception_type(error: BaseException) -> str:
    cls = type(error)
    module = type.__getattribute__(cls, "__module__")
    name = type.__getattribute__(cls, "__qualname__")
    return type.__getattribute__(cls, "__name__") if module == "builtins" else f"{module}.{name}"


def _exception_attr(error: BaseException, name: str, default=None):
    """Read native exception fields without invoking a subclass's properties."""
    try:
        descriptor = BaseException.__dict__.get(name)
        if descriptor is None and issubclass(type(error), BaseExceptionGroup):
            descriptor = BaseExceptionGroup.__dict__.get(name)
        if descriptor is not None:
            return descriptor.__get__(error, type(error))
        return getattr(error, name, default)
    except Exception:
        return default


def capture_error(
    error: BaseException | str,
    *,
    phase: str,
    kind: str = "fatal",
    context: dict | None = None,
    secrets: Iterable[str] = (),
    code: str | None = None,
    message: str | None = None,
) -> dict:
    """Capture an error where it occurs, preserving causes without capturing locals.

    ``message`` lets adapters use the provider's error message instead of SDK
    exception strings that may embed an entire HTTP response body.
    """
    sensitive = _secrets(secrets)
    chain = []
    seen: set[int] = set()
    is_exception = issubclass(type(error), BaseException)
    current = error if is_exception else None
    relation = "error"
    while current is not None and id(current) not in seen and len(chain) < _MAX_CHAIN:
        seen.add(id(current))
        frames = []
        tb = _exception_attr(current, "__traceback__")
        while tb is not None:
            frames.append({
                "file": tb.tb_frame.f_code.co_filename,
                "line": tb.tb_lineno,
                "function": tb.tb_frame.f_code.co_name,
            })
            tb = tb.tb_next
        node = {
            "exception_type": _exception_type(current),
            "message": safe_text(message if not chain and message is not None else _text(current), secrets=sensitive),
            "relation": relation,
            "frames": frames[-_MAX_FRAMES:],
        }
        if len(frames) > _MAX_FRAMES:
            node["omitted_frames"] = len(frames) - _MAX_FRAMES
        if issubclass(type(current), BaseExceptionGroup):
            node["exceptions"] = [
                {"exception_type": _exception_type(child), "message": safe_text(child, secrets=sensitive)}
                for child in _exception_attr(current, "exceptions", ())[:16]
            ]
        chain.append(node)
        cause = _exception_attr(current, "__cause__")
        if cause is not None:
            current, relation = cause, "cause"
        elif not _exception_attr(current, "__suppress_context__", False):
            current, relation = _exception_attr(current, "__context__"), "context"
        else:
            current = None

    traceback = []
    for node in reversed(chain):
        if traceback:
            traceback.append("\nThe above exception was the cause or context of the following exception:\n")
        if node["frames"]:
            traceback.append("Traceback (most recent call last):")
            for frame in node["frames"]:
                traceback.append(f'  File "{frame["file"]}", line {frame["line"]}, in {frame["function"]}')
        traceback.append(f'{node["exception_type"]}: {node["message"]}')
    record = {
        "schema_version": 1,
        "error_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "code": code or re.sub(r"[^A-Z0-9]+", "_", safe_text(phase).upper()).strip("_") + "_FAILED",
        "phase": phase,
        "kind": kind,
        "message": message if message is not None else _text(error),
        "exception_type": _exception_type(error) if is_exception else None,
        "traceback": "\n".join(traceback) if chain else None,
        "cause_chain": chain,
        "context": context if context is not None else {},
    }
    if current is not None:
        record["chain_truncated"] = True
    return sanitize(record, secrets=sensitive)


def error_summary(details: dict, *, t=None) -> str:
    """One readable message with a stable reference to the complete record."""
    from .i18n import Translator

    t = t or Translator()
    details = sanitize(details)
    if not isinstance(details, dict):
        return t("harness.diagnostics.unavailable")
    context = details.get("context")
    context = context if isinstance(context, dict) else {}
    location = " / ".join(str(context[key]) for key in ("agent", "model", "tool_name") if context.get(key))
    label = f"[{details.get('code', 'ERROR')} {details.get('error_id', 'unknown')}]"
    text = safe_text(details.get("message") or details.get("exception_type") or t("harness.diagnostics.unknown_error"))
    text = " ".join(text.split())
    exception_type = details.get("exception_type")
    if exception_type and not text.startswith(str(exception_type) + ":"):
        text = f"{exception_type}: {text}"
    if len(text) > 500:
        text = text[:500] + "…"
    summary = f"{label} {details.get('phase', 'unknown')}"
    if location:
        summary += f" ({location})"
    summary += f": {text}"
    if details.get("log_path"):
        summary += "\n" + t("harness.diagnostics.path", path=details["log_path"])
    elif details.get("log_expired"):
        summary += "\n" + t("harness.diagnostics.rotated", id=details.get("error_id", ""))
    elif details.get("logging_error"):
        summary += "\n" + t("harness.diagnostics.unsaved")
    return safe_text(summary)


def emergency_write(details: dict, reporting_error=None) -> None:
    """Last-resort reporting: never mask a failure with another reporting error."""
    try:
        import json

        record = dict(details)
        if reporting_error is not None:
            record["reporting_error"] = safe_text(reporting_error)
        sys.stderr.write("virgo diagnostic: " + json.dumps(sanitize(record), ensure_ascii=True) + "\n")
        sys.stderr.flush()
    except Exception:
        # A closed/broken stderr is the end of the reporting chain.
        pass
