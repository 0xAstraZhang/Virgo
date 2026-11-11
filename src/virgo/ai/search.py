"""Small, provider-independent helpers for hosted search responses."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .types import RawContent, TextContent


def is_search_unsupported(error: Exception) -> bool:
    """Only explicit capability rejection permits a retry without hosted search."""
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
    if not re.search(r"web[_ ]search", text):
        return False
    if code in {"unsupported_tool", "unknown_tool"}:
        return True
    return bool(re.search(
        r"web[_ ]search(?:_\d+)?['\"` ]*(?:is |tool is )?(?:not supported|unsupported|not enabled|"
        r"not permitted|not allowed|not available (?:for|with|on))|"
        r"(?:does not support|unsupported|unknown tool|unrecognized tool|invalid tool type)"
        r"[^.\n]{0,60}web[_ ]search|"
        r"invalid value[^.\n]{0,40}web_search[^\n]{0,100}supported values", text
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
            if data.get("type") == "server_tool_use" and data.get("name") == "web_search":
                query = data.get("input", {}).get("query")
                if isinstance(query, str):
                    queries.append(query[:2000])
            if data.get("type") == "web_search_call":
                action = data.get("action") or {}
                citations = action.get("sources") or []
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
    if message.search_started and message.stop_reason in {"error", "aborted"}:
        note = "\n\n[Web search was interrupted after it started; any sources above are partial results."
        if queries:
            note += " Queries: " + json.dumps(list(dict.fromkeys(queries)), ensure_ascii=False)[:4000]
        note += "]"
        if not any(isinstance(b, TextContent) and b.text == note for b in message.content):
            message.content.append(TextContent(note))
