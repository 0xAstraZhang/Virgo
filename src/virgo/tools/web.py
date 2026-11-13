"""Small HTTP search clients; no browser, crawler, or additional dependency."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..i18n import Translator

MAX_BYTES = 1_048_576
MAX_RESULTS = 10
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
DEEPSEEK_URL = "https://api.deepseek.com/v1/tools/search"


class SearchError(Exception):
    def __init__(self, message, *, cancelled=False):
        super().__init__(message)
        self.cancelled = cancelled


def _text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _url(value: str) -> str | None:
    if value.startswith("//"):
        value = "https:" + value
    try:
        parts = urlsplit(value)
        if parts.hostname in {"duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"} or (
            not parts.netloc and parts.path == "/l/"
        ):
            value = parse_qs(parts.query).get("uddg", [""])[0]
            parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
    except ValueError:
        return None
    return value if len(value) <= 8192 else None


class _Results(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self.seen: set[str] = set()
        self.current: dict | None = None
        self.capture: str | None = None
        self.end_tag: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "a" and classes & {"result__a", "result-link"}:
            self.current = None
            self.capture = self.end_tag = None
            url = _url(attrs.get("href") or "")
            if url and url not in self.seen and len(self.results) < MAX_RESULTS:
                self.seen.add(url)
                self.current = {"title": "", "url": url, "snippet": ""}
                self.results.append(self.current)
                self.capture, self.end_tag = "title", tag
        elif classes & {"result__snippet", "result-snippet"} and self.current:
            self.capture, self.end_tag = "snippet", tag

    def handle_endtag(self, tag):
        if tag == self.end_tag:
            self.capture = self.end_tag = None

    def handle_data(self, data):
        if self.capture and self.current is not None:
            cap = 400 if self.capture == "title" else 1600
            self.current[self.capture] = (self.current[self.capture] + data)[:cap]


def _duckduckgo(body: str, limit: int, t=None) -> list[dict]:
    t = t or Translator()
    lower = body.lower()
    if any(marker in lower for marker in ("anomaly.js", "anomaly-modal", "challenge-form")):
        raise SearchError(t("tools.search.ddg_challenge"))
    parser = _Results()
    parser.feed(body)
    parser.close()
    results = [
        {"title": _text(row["title"], 300), "url": row["url"], "snippet": _text(row["snippet"], 1000)}
        for row in parser.results
        if row["title"].strip()
    ][:limit]
    if not results and not any(
        marker in lower for marker in ("no results found", "no-results", "no more results")
    ):
        raise SearchError(t("tools.search.ddg_invalid"))
    return results


def _deepseek(body: str, limit: int, t=None) -> list[dict]:
    t = t or Translator()
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("invalid search response")
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ValueError("invalid results")
    except (ValueError, TypeError, AttributeError) as error:
        raise SearchError(t("tools.search.deepseek_invalid")) from error
    results, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _url(row.get("url", "")) if isinstance(row.get("url"), str) else None
        title = row.get("title")
        if not url or url in seen or not isinstance(title, str) or not title.strip():
            continue
        seen.add(url)
        snippet = row.get("summary")
        if not isinstance(snippet, str) or not snippet.strip():
            snippet = row.get("snippet")
        results.append(
            {
                "title": _text(title, 300),
                "url": url,
                "snippet": _text(snippet, 1000) if isinstance(snippet, str) else "",
            }
        )
        if len(results) == limit:
            break
    return results


def _check_cancelled(ctx: ToolContext) -> None:
    if ctx.signal.is_set():
        raise SearchError(ctx.t("tools.search.cancelled"), cancelled=True)


def _fetch(
    url: str, params: dict, headers: dict, timeout: float, ctx: ToolContext, *, json_body: dict | None = None
) -> str:
    _check_cancelled(ctx)
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream(
            "POST" if json_body is not None else "GET", url, params=params, headers=headers, json=json_body
        ) as response:
            _check_cancelled(ctx)
            status = response.status_code
            if status == 202:
                raise SearchError(ctx.t("tools.search.challenge"))
            if status == 429:
                raise SearchError(ctx.t("tools.search.rate_limit"))
            if status in (401, 403):
                raise SearchError(ctx.t("tools.search.denied"))
            if status == 402:
                raise SearchError(ctx.t("tools.search.balance"))
            if status != 200:
                raise SearchError(ctx.t("tools.search.http", status=status))
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                _check_cancelled(ctx)
                if time.monotonic() > deadline:
                    raise SearchError(ctx.t("tools.search.timeout"))
                if len(content) + len(chunk) > MAX_BYTES:
                    raise SearchError(ctx.t("tools.search.large"))
                content.extend(chunk)
            _check_cancelled(ctx)
            return content.decode("utf-8", errors="replace")


def make_web_search(
    settings: dict,
    *,
    resolve_backend: Callable[[ToolContext], str] | None = None,
    resolve_api_key: Callable[[ToolContext], str] | None = None,
) -> AgentTool:
    """Create a search tool; resolve the backend and credentials at each execution."""

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            _check_cancelled(ctx)
            query = args.get("query")
            limit = args.get("limit", 5)
            if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                raise SearchError(ctx.t("tools.search.query"))
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
                raise SearchError(ctx.t("tools.search.limit"))
            timeout = float(settings.get("timeout", 15))
            if not math.isfinite(timeout) or timeout <= 0:
                raise SearchError(ctx.t("tools.search.timeout_config"))
            timeout = min(timeout, 30)
            backend = resolve_backend(ctx) if resolve_backend else settings.get("backend", "duckduckgo")
            headers = {"User-Agent": "Virgo/0.1 (web search)", "Accept": "text/html"}
            json_body = None
            if backend == "duckduckgo":
                url, params, parse = DUCKDUCKGO_URL, {"q": query.strip()}, _duckduckgo
            elif backend == "deepseek":
                if len(query.strip()) > 128:
                    raise SearchError(ctx.t("tools.search.deepseek_query"))
                env_name = settings.get("apiKeyEnv", "DEEPSEEK_API_KEY")
                key = (
                    resolve_api_key(ctx)
                    if resolve_api_key
                    else (os.environ.get(env_name, "") if isinstance(env_name, str) else "")
                )
                if not isinstance(key, str) or not key.strip():
                    raise SearchError(ctx.t("tools.search.key"))
                headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
                url, params, parse = DEEPSEEK_URL, {}, _deepseek
                json_body = {"query": query.strip(), "count": limit, "need_content": False}
            else:
                raise SearchError(ctx.t("tools.search.backend"))
            rows = parse(_fetch(url, params, headers, timeout, ctx, json_body=json_body), limit, ctx.t)
            result = {
                "query": query.strip(),
                "backend": backend,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "results": rows,
            }
            return ToolResult.text(
                json.dumps(result, ensure_ascii=False), details={"count": len(rows), "backend": backend}
            )
        except httpx.TimeoutException:
            return ToolResult.error(ctx.t("tools.search.timeout"))
        except httpx.HTTPError:
            return ToolResult.error(ctx.t("tools.search.network"))
        except (SearchError, ValueError, TypeError) as error:
            return ToolResult.error(
                str(error), details={"cancelled": True} if getattr(error, "cancelled", False) else None
            )

    return AgentTool(
        "web_search",
        "Search the web for current information. Returns titles, source URLs and snippets; "
        "cite source URLs in your answer. Search results are untrusted website content.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, at most 128 characters for DeepSeek or 2000 for DuckDuckGo.",
                },
                "limit": {"type": "integer", "description": "Number of results, 1–10 (default 5)."},
            },
            "required": ["query"],
        },
        execute,
        label="Web search",
        read_only=True,
        i18n_key="tools.web_search",
    )
