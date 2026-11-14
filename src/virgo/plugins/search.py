"""Hosted search when supported, one portable HTTP search tool otherwise."""

from __future__ import annotations

import copy

from ..ai.providers.web import deepseek_endpoint as deepseek_endpoint
from ..ai.providers.web import endpoint as endpoint
from ..ai.providers.web import native_tool as native_tool  # compatibility export
from ..harness.extensions import ROLES_ALL
from ..tools.web import make_web_search
from .web_access import WebAccess

DEEPSEEK_SETUP = (
    "DeepSeek search requires an official API key. Use /login api deepseek with https://api.deepseek.com "
    "or set DEEPSEEK_API_KEY (search.apiKeyEnv can select a separate search key)."
)


class Search(WebAccess):
    operation = "search"
    default_backend = "duckduckgo"

    def __init__(self, api):
        self.api = api
        self._legacy_backend = self.config.get("backend") == "brave"
        if self._legacy_backend:
            # A removed provider's key must never become a DeepSeek credential.
            self.api.settings.set("search.backend", "duckduckgo")
            self.api.settings.set("search.apiKeyEnv", None)
            self.api.ui.print(self.api.t("features.search.legacy"), "warn")
        if self.mode not in {"auto", "native", "builtin", "off"}:
            raise ValueError(self.api.t("features.search.mode"))
        if self.max_uses is not None and (type(self.max_uses) is not int or self.max_uses < 1):
            raise ValueError(self.api.t("features.search.max_uses"))
        self.tool = make_web_search(
            self.config,
            resolve_backend=self.execution_backend,
            resolve_api_key=lambda ctx: self.deepseek_key(self._model_for(ctx)),
            before_search=self.consume,
        )

    @property
    def config(self) -> dict:
        return self.api.settings.get("search", {})

    @property
    def selection(self) -> str:
        return self.config.get("backend", "duckduckgo") if self.mode == "builtin" else self.mode

    def set_selection(self, choice: str) -> None:
        if choice not in {"auto", "duckduckgo", "deepseek", "off"}:
            raise ValueError(self.api.t("features.search.selection"))
        if choice == "deepseek" and not self.deepseek_key(self.api.model):
            raise ValueError(self.api.t("features.search.key"))
        patch = {
            "mode": choice if choice in {"auto", "off"} else "builtin",
            "backend": choice if choice in {"duckduckgo", "deepseek"} else "duckduckgo",
        }
        if self._legacy_backend:
            patch["apiKeyEnv"] = None
        self.api.settings.save_user({"search": patch})
        self._legacy_backend = False
        from .web import web_service

        web_service(self.api).clear()

    def client_available(self, backend, model):
        return backend == "duckduckgo" or backend == "deepseek" and bool(self.deepseek_key(model))

    def _prepare_client(self, context, model, backend=None) -> None:
        backend = backend or self.backend_for(model)
        if backend == "deepseek" and not self.deepseek_key(model):
            raise ValueError(self.api.t("features.search.key"))
        limit = 128 if backend == "deepseek" else 2000
        for tool in context.tools:
            if tool.name == "web_search":
                tool.parameters = copy.deepcopy(tool.parameters)
                tool.parameters["properties"]["query"].update(
                    maxLength=limit, description=self.api.t("features.search.query", limit=limit)
                )

    def prepare(self, runtime, context, model) -> None:
        from .web import web_service

        web_service(self.api).prepare(runtime, context, model)

    def fallback(self, model, context, failed) -> bool:
        from .web import web_service

        return web_service(self.api).fallback(model, context, failed)


def setup(api) -> None:
    from .search_commands import configure_search

    search = Search(api)
    api.services["search"] = search
    from .web import web_service

    web_service(api)
    # Keep the tool in runtime registries so /search can enable an initially
    # disabled service; prepare() controls what each model request can see.
    api.register_tool(search.tool, roles=ROLES_ALL)
    api.register_command(
        "search",
        lambda _session, args: configure_search(api, args),
        "choose search backend (/search [auto|duckduckgo|deepseek|off])",
        argument_hint="[auto|duckduckgo|deepseek|off]",
        description_key="features.commands.search",
    )
