"""Web plugin entry point: coordinates search and page reading capabilities."""

from __future__ import annotations

from .fetch import setup as setup_fetch
from .search import setup as setup_search
from .service import GUIDANCE, Web, web_service

__all__ = ["GUIDANCE", "Web", "setup", "web_service"]


def setup(api):
    """Search and page reading share routing, permissions and the optional call budget."""
    web_service(api)
    if api.settings.get("plugins.web.searchEnabled", True):
        setup_search(api)
    if api.settings.get("plugins.web.fetchEnabled", True):
        setup_fetch(api)
