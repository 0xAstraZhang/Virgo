"""Provider-neutral web capability offers. Adapters own protocol and model knowledge."""

from __future__ import annotations

import copy
from collections.abc import Callable

from .providers.web import deepseek_endpoint, endpoint
from .providers.web import offers as _builtin
from .types import Model, WebOffer

# Extensions can append an adapter, without changing selection or permission logic.
ADAPTERS: list[Callable[[Model], list[WebOffer]]] = [_builtin]

__all__ = [
    "ADAPTERS",
    "WebOffer",
    "declarations",
    "deepseek_endpoint",
    "endpoint",
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
