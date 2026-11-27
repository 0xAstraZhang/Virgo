"""Provider-neutral web capability offers, rejection matching, and source formatting."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from .providers.web import deepseek_endpoint, endpoint
from .providers.web import offers as _builtin
from .types import Model, RawContent, TextContent, WebOffer

# Extensions can append an adapter, without changing selection or permission logic.
ADAPTERS: list[Callable[[Model], list[WebOffer]]] = [_builtin]

__all__ = [
    "ADAPTERS",
    "WebOffer",
    "add_source_links",
    "declarations",
    "deepseek_endpoint",
    "endpoint",
    "is_web_unsupported",
    "native_event",
    "offers_for",
    "started",
]


def offers_for(model: Model) -> list[WebOffer]:
    offers = [offer for adapter in ADAPTERS for offer in adapter(model)]
    if len({o.id for o in offers}) != len(offers):
        raise ValueError("Web adapter offer IDs must be unique")
    for offer in offers:
        if not offer.covers or not offer.covers <= {"search", "fetch", "find"}:
            raise ValueError(f"Invalid web capabilities for {offer.id}")
        if offer.hosted == (offer.backend is not None):
            raise ValueError(f"Web offer {offer.id} needs exactly one declaration or client backend")
    return offers


def declarations(context) -> list[dict]:
    """Return detached copies of hosted web tool declarations."""
    return copy.deepcopy(context.web_tools)


def started(message) -> bool:
    return bool(message.native_started)


def native_event(data: dict) -> dict | None:
    """Portable UI metadata; keep provider payloads in RawContent, never dump PDF bytes."""
    kind = data.get("type", "")
    if kind == "web_search_call":
        action = data.get("action") or {}
        name = {"open_page": "web_fetch", "find_in_page": "web_find", "find": "web_find"}.get(action.get("type"), "web_search")
        return {"name": name, "id": data.get("id"), "args": action, "status": data.get("status", "in_progress")}
    if kind == "server_tool_use":
        return {"name": data.get("name", "web"), "id": data.get("id"), "args": data.get("input", {}), "status": "in_progress"}
    if kind in {"web_search_tool_result", "web_fetch_tool_result"}:
        content = data.get("content") or {}
        error = isinstance(content, dict) and "error" in content.get("type", "")
        details = copy.deepcopy(content)
        if kind == "web_fetch_tool_result" and isinstance(details, dict):
            document = details.get("content") or {}
            if isinstance(document, dict) and "source" in document:
                source = document["source"]
                text = source.get("data", "")
                source["data"] = ("[PDF document]" if source.get("type") != "text" else
                                  text[:2000] + ("\n[Preview truncated]" if len(text) > 2000 else ""))
        return {"name": kind.removesuffix("_tool_result"), "id": data.get("tool_use_id"), "args": {},
                "status": "failed" if error else "completed", "result": details}
    return None


def is_web_unsupported(error: Exception, capability: str) -> bool:
    """Only explicit tool rejection, never page errors, permits capability fallback."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
    code = body.get("code") or body.get("type") if isinstance(body, dict) else None
    if status not in (400, 404, 422) and not (
        status is None and code in {"unsupported_tool", "unsupported_value", "unknown_tool", "invalid_request_error"}
    ):
        return False
    text = (str(error) + " " + json.dumps(body, default=str)).lower()
    pattern = r"web[_ ]" + re.escape(capability)
    if not re.search(pattern, text):
        return False
    if code in {"unsupported_tool", "unknown_tool"}:
        return True
    return bool(re.search(
        pattern + r"(?:_\d+)?['\"` ]*(?:is |tool is )?(?:not supported|unsupported|not enabled|"
        r"not permitted|not allowed|not available (?:for|with|on))|"
        r"(?:does not support|unsupported|unknown tool|unrecognized tool|invalid tool type)"
        r"[^.\n]{0,60}" + pattern + r"|invalid value[^.\n]{0,40}" + pattern + r"[^\n]{0,100}supported values", text
    ))


def add_source_links(message) -> None:
    """Keep citations visible and portable even when the next turn uses another API."""
    sources = {}
    queries = []
    for block in message.content:
        citations = block.citations if isinstance(block, TextContent) else []
        if isinstance(block, RawContent):
            data = block.data
            if data.get("type") == "web_search_tool_result" and isinstance(data.get("content"), list):
                citations = data["content"]
            if data.get("type") == "web_fetch_tool_result" and isinstance(data.get("content"), dict):
                fetched = data["content"]
                document = fetched.get("content") or {}
                if fetched.get("type") == "web_fetch_result":
                    citations = [{"url": fetched.get("url"), "title": document.get("title")}]
            if data.get("type") == "server_tool_use" and data.get("name") == "web_search":
                query = data.get("input", {}).get("query")
                if isinstance(query, str):
                    queries.append(query[:2000])
            if data.get("type") == "web_search_call":
                action = data.get("action") or {}
                citations = list(action.get("sources") or [])
                if action.get("url"):
                    citations.append({"url": action["url"]})
                query = action.get("query")
                if isinstance(query, str):
                    queries.append(query[:2000])
                queries.extend(q[:2000] for q in action.get("queries", []) if isinstance(q, str))
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            url = citation.get("url", "")
            if not isinstance(url, str):
                continue
            try:
                valid = urlsplit(url).scheme in {"http", "https"} and urlsplit(url).hostname
            except ValueError:
                valid = False
            if valid and not any(c.isspace() for c in url):
                title = str(citation.get("title") or url).replace("\n", " ").replace("\r", " ")
                title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
                sources[url] = title
    if sources:
        links = "\n\nSources:\n" + "\n".join(
            f"- [{title}](<{url.replace('<', '%3C').replace('>', '%3E')}>)" for url, title in sources.items()
        )
        if not any(isinstance(b, TextContent) and b.text == links for b in message.content):
            message.content.append(TextContent(links))
    if message.native_started and message.stop_reason in {"error", "aborted"}:
        has_search = any(
            isinstance(b, RawContent) and (
                b.data.get("type") in {"web_search_call", "web_search_tool_result"}
                or b.data.get("type") == "server_tool_use" and b.data.get("name") == "web_search"
            )
            for b in message.content
        )
        has_fetch = any(
            isinstance(b, RawContent) and (
                b.data.get("type") == "web_fetch_tool_result"
                or b.data.get("type") == "server_tool_use" and b.data.get("name") == "web_fetch"
            )
            for b in message.content
        )
        operation = "Web search" if has_search or not has_fetch else "Web retrieval"
        note = f"\n\n[{operation} was interrupted after it started; any sources above are partial results."
        if queries:
            note += " Queries: " + json.dumps(list(dict.fromkeys(queries)), ensure_ascii=False)[:4000]
        note += "]"
        if not any(isinstance(b, TextContent) and b.text == note for b in message.content):
            message.content.append(TextContent(note))
