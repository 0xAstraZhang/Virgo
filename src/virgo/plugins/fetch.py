"""Fetch configuration and client execution; native routing is shared with search."""

from __future__ import annotations

from ..agent.types import ToolCallEvent
from ..harness.extensions import ROLES_ALL
from ..tools.web_fetch import FetchError, make_web_fetch
from .web import web_service
from .web_access import WebAccess


class Fetch(WebAccess):
    operation = "fetch"
    default_backend = "http"

    def __init__(self, api):
        self.api = api
        if self.mode not in {"auto", "native", "builtin", "off"}:
            raise ValueError(self.api.t("features.fetch.mode"))
        if self.max_uses is not None and (type(self.max_uses) is not int or self.max_uses < 1):
            raise ValueError(self.api.t("features.fetch.max_uses"))
        tokens = self.config.get("maxContentTokens", 8000)
        if type(tokens) is not int or not 1 <= tokens <= 100000:
            raise ValueError(self.api.t("features.fetch.max_tokens"))
        self.tool = make_web_fetch(
            lambda: self.config,
            resolve_backend=self.execution_backend,
            resolve_api_key=lambda ctx: self.deepseek_key(self._model_for(ctx)),
            before_fetch=self.consume,
            review_redirect=self._redirect,
        )

    @property
    def config(self):
        return self.api.settings.get("fetch", {})

    @property
    def selection(self):
        return self.config.get("backend", "http") if self.mode == "builtin" else self.mode

    def client_available(self, backend, model):
        return backend == "http" or backend == "deepseek" and bool(self.deepseek_key(model))

    def _prepare_client(self, context, model, backend=None):
        backend = backend or self.backend_for(model)
        if backend not in {"http", "deepseek"}:
            raise ValueError(self.api.t("features.fetch.backend"))
        if not self.client_available(backend, model):
            raise ValueError(self.api.t("features.fetch.key"))

    def set_selection(self, choice):
        if choice not in {"auto", "http", "deepseek", "off"}:
            raise ValueError(self.api.t("features.fetch.selection"))
        if choice == "deepseek" and not self.deepseek_key(self.api.model):
            raise ValueError(self.api.t("features.fetch.key"))
        self.api.settings.save_user(
            {
                "fetch": {
                    "mode": choice if choice in {"auto", "off"} else "builtin",
                    "backend": choice if choice in {"http", "deepseek"} else "http",
                }
            }
        )
        web_service(self.api).clear()

    def _redirect(self, url, ctx):
        ev = ToolCallEvent(
            ctx.call_id + "-redirect",
            "web_fetch",
            {"url": url},
            self.tool,
            ctx.agent,
            ctx.cwd,
            ctx.signal,
            ctx.review_source,
        )
        blocked = self.api.authorize_tool(ev)
        if blocked:
            raise FetchError(blocked.reason)
        return ev.args["url"]


def setup(api):
    from .fetch_commands import configure_fetch

    fetch = Fetch(api)
    api.services["fetch"] = fetch
    web_service(api)
    api.register_tool(fetch.tool, roles=ROLES_ALL)
    api.register_command(
        "fetch",
        lambda _session, args: configure_fetch(api, args),
        "choose page reader (/fetch [auto|http|deepseek|off])",
        description_key="features.commands.fetch",
    )
