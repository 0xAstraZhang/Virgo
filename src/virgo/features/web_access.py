"""Shared web permission, request binding and credential policy, independent of routing."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os

from ..agent.types import ToolCallEvent
from ..ai.providers.web import deepseek_endpoint, endpoint
from ..ai.types import UserMessage
from ..ai.web import offers_for
from .permission import parse_rule


class WebAccess:
    operation: str
    default_backend: str

    @property
    def mode(self) -> str:
        return self.config.get("mode", "auto")

    @property
    def max_uses(self) -> int:
        return self.config.get("maxUses", 3)

    def deepseek_key(self, model=None) -> str:
        """Read credentials at use time; never send a proxy's model key to DeepSeek."""
        env_name = self.config.get("apiKeyEnv")
        if env_name is not None:
            key = os.environ.get(env_name, "") if isinstance(env_name, str) else ""
            return key.strip()
        candidate = model if model is not None and deepseek_endpoint(model) else self.session.resolve("deepseek/_search")
        if not deepseek_endpoint(candidate):
            return ""
        credential = self.session.auth.all().get(candidate.provider)
        if credential:
            if credential.get("type") != "api_key":
                return ""
            connection = credential.get("connection")
            if connection is not None and (
                not isinstance(connection, dict) or connection.get("api") != candidate.api
                or str(connection.get("baseUrl", "")).rstrip("/") != (candidate.base_url or "").rstrip("/")
            ):
                return ""
            key = credential.get("key")
        else:
            key = candidate.options.get("apiKey") or os.environ.get(candidate.api_key_env or "", "")
        return key.strip() if isinstance(key, str) else ""

    def _model_for(self, ctx):
        runtime = getattr(ctx.review_source, "runtime", None)
        if runtime is None:
            if ctx.agent != "main":
                raise ValueError(self.session.t('features.web.context', operation=self.operation))
            runtime = self.session.main
        # Retries can use a different model without changing runtime.model. Keep
        # that request's choice local to its runtime, including parallel agents.
        original, request = getattr(runtime, "_web_models", (runtime.model, runtime.model))
        return request if original is runtime.model else runtime.model

    def key(self, model) -> tuple:
        credential = self.session.auth.all().get(model.provider, {})
        # Invalidate on account/key changes without retaining secrets in the cache.
        identity = json.dumps(credential, sort_keys=True, default=str)
        identity += str(model.options.get("apiKey") or os.environ.get(model.api_key_env or "", ""))
        return (model.provider, endpoint(model), model.api, model.id,
                hashlib.sha256(identity.encode()).hexdigest())

    def _permitted(self, runtime, model, key) -> bool:
        permissions = self.session.services.get("permissions")
        if (self.session.bus.has_handlers("tool_call", exclude=permissions.on_tool_call if permissions else None)
                or self.session.bus.has_handlers("tool_result")):
            return False  # Native queries cannot pass through client-tool hooks.
        if permissions is None:
            return True
        mode = permissions.mode_for(runtime.name)
        if mode == "accept-all":
            return True
        rules = self.session.settings.get("permissions", {})
        # The provider chooses queries internally. Scoped rules must be enforced on
        # actual queries through the client tool, never guessed from the user's prompt.
        for rule in [*rules.get("allow", []), *rules.get("deny", [])]:
            tool, pattern = parse_rule(rule)
            if pattern is not None and fnmatch.fnmatchcase(f"web_{self.operation}", tool):
                return False
        request = next((m for m in reversed(runtime.messages) if isinstance(m, UserMessage) and m.kind is None), None)
        ev = ToolCallEvent(
            call_id=f"native-{self.operation}-{runtime.name}-{id(request)}", tool_name=f"web_{self.operation}",
            args={"native": True, "provider": model.provider, "request": request.text() if request else "",
             "scope": f"Allow the provider to perform public web {self.operation} operations for this user request."},
            tool=self.tool, cwd=runtime.cwd, agent=runtime.name, signal=runtime.agent.signal,
            review_source=runtime.review_source,
        )
        from .permission_context import review_context_key

        cache_key = (key, mode, json.dumps(rules, sort_keys=True), id(request), review_context_key(self.session, ev))
        if getattr(runtime, "_web_reviews", {}).get(self.operation) is not None and runtime._web_reviews[self.operation][0] == cache_key:
            return runtime._web_reviews[self.operation][1]
        # Review once per user request. If the broad scope cannot be approved,
        # auto mode offers the builtin tool for exact-query review instead.
        decision, _reason = permissions.decide(ev)
        approved = decision == "allow"
        runtime._web_reviews = {**getattr(runtime, "_web_reviews", {}), self.operation: (cache_key, approved)}
        return approved

    def execution_backend(self, ctx):
        runtime = getattr(ctx.review_source, "runtime", None)
        if runtime is None and ctx.agent == "main":
            runtime = self.session.main
        model = self._model_for(ctx)
        binding = getattr(runtime, "_web_models", None)
        backends = getattr(runtime, "_web_backends", {})
        config = getattr(runtime, "_web_backend_config", {})
        if binding and binding[0] is runtime.model and config.get(self.operation) == self.config and self.operation in backends:
            backend = backends[self.operation]
            if backend is None:
                raise ValueError(self.session.t('features.web.client_disabled', operation=self.operation))
            return backend
        return self.backend_for(model)

    def backend_for(self, model) -> str:
        if self.mode == "off":
            raise ValueError(self.session.t('features.web.disabled', operation=self.operation))
        if self.mode != "builtin" and model.options.get("native" + self.operation.title()) is not False:
            for offer in offers_for(model):
                if (offer.native and not offer.hosted and offer.capabilities == {self.operation}
                        and self.client_available(offer.backend, model)):
                    return offer.backend
        if self.mode == "native":
            raise ValueError(self.session.t('features.web.native_client', operation=self.operation))
        return self.config.get("backend", self.default_backend)
