"""Fetch configuration and client execution; native routing is shared with search."""

from __future__ import annotations

import threading
import weakref

from ..agent.types import ToolCallEvent
from ..ai.types import UserMessage
from ..harness.extensions import ROLES_ALL
from ..tools.web_fetch import FetchError, make_web_fetch
from .web import web_service
from .web_access import WebAccess


class Fetch(WebAccess):
    operation = "fetch"
    default_backend = "http"

    def __init__(self, api):
        self.session = api.session
        self._lock = threading.Lock()
        self._uses = weakref.WeakKeyDictionary()
        if self.mode not in {"auto", "native", "builtin", "off"}:
            raise ValueError(self.session.t("features.fetch.mode"))
        if type(self.max_uses) is not int or not 1 <= self.max_uses <= 10:
            raise ValueError(self.session.t("features.fetch.max_uses"))
        tokens = self.config.get("maxContentTokens", 8000)
        if type(tokens) is not int or not 1 <= tokens <= 100000:
            raise ValueError(self.session.t("features.fetch.max_tokens"))
        self.tool = make_web_fetch(
            lambda: self.config,
            resolve_backend=self.execution_backend,
            resolve_api_key=lambda ctx: self.deepseek_key(self._model_for(ctx)),
            before_fetch=self._consume,
            review_redirect=self._redirect,
        )

    @property
    def config(self):
        return self.session.settings.get("fetch", {})

    @property
    def selection(self):
        return self.config.get("backend", "http") if self.mode == "builtin" else self.mode

    def client_available(self, backend, model):
        return backend == "http" or backend == "deepseek" and bool(self.deepseek_key(model))

    def _prepare_client(self, context, model, backend=None):
        backend = backend or self.backend_for(model)
        if backend not in {"http", "deepseek"}:
            raise ValueError(self.session.t("features.fetch.backend"))
        if not self.client_available(backend, model):
            raise ValueError(self.session.t("features.fetch.key"))

    def set_selection(self, choice):
        if choice not in {"auto", "http", "deepseek", "off"}:
            raise ValueError(self.session.t("features.fetch.selection"))
        if choice == "deepseek" and not self.deepseek_key(self.session.model):
            raise ValueError(self.session.t("features.fetch.key"))
        self.session.settings.save_user(
            {
                "fetch": {
                    "mode": choice if choice in {"auto", "off"} else "builtin",
                    "backend": choice if choice in {"http", "deepseek"} else "http",
                }
            }
        )
        web_service(self.session).clear()

    def _consume(self, ctx):
        runtime = getattr(ctx.review_source, "runtime", None)
        if runtime is None:
            if ctx.agent != "main":
                raise FetchError(self.session.t("features.fetch.context"))
            runtime = self.session.main
        request = next(
            (m for m in reversed(runtime.messages) if isinstance(m, UserMessage) and m.kind is None), None
        )
        with self._lock:
            previous, count = self._uses.get(runtime, (None, 0))
            count = count if previous is request else 0
            if count >= self.max_uses:
                raise FetchError(self.session.t("features.fetch.limit"))
            self._uses[runtime] = (request, count + 1)

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
        blocked = self.session._before_tool(ev) or self.session._before_tool_execute(ev)
        if blocked:
            raise FetchError(blocked.reason)
        return ev.args["url"]


def setup(api):
    from ..harness.fetch_commands import configure_fetch

    fetch = Fetch(api)
    api.services["fetch"] = fetch
    web_service(api.session)
    api.register_tool(fetch.tool, roles=ROLES_ALL)
    api.register_command(
        "fetch",
        configure_fetch,
        "choose page reader (/fetch [auto|http|deepseek|off])",
        description_key="features.commands.fetch",
    )
