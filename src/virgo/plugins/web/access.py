"""Shared web permission, request binding and credential policy, independent of routing."""

from __future__ import annotations

import hashlib
import json
import os

from ...agent.types import ToolCallEvent
from ...ai.providers.web import deepseek_endpoint, endpoint
from ...ai.types import UserMessage
from ...ai.web import offers_for


class WebAccess:
    operation: str
    default_backend: str

    @property
    def mode(self) -> str:
        return self.config.get("mode", "auto")

    @property
    def max_uses(self) -> int | None:
        return self.config.get("maxUses")

    def _runtime(self, ctx):
        runtime = self.api.runtime_for_context(ctx)
        if runtime is None:
            raise ValueError(self.api.t("features.web.context", operation=self.operation))
        return runtime

    def consume(self, ctx):
        from . import web_service

        web_service(self.api).consume(self._runtime(ctx), self.operation, self.max_uses)

    def deepseek_key(self, model=None) -> str:
        """Read credentials at use time; never send a proxy's model key to DeepSeek."""
        env_name = self.config.get("apiKeyEnv")
        if env_name is not None:
            key = os.environ.get(env_name, "") if isinstance(env_name, str) else ""
            return key.strip()
        candidate = model if model is not None and deepseek_endpoint(model) else self.api.resolve("deepseek/_search")
        if not deepseek_endpoint(candidate):
            return ""
        credential = self.api.auth.all().get(candidate.provider)
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
        runtime = self._runtime(ctx)
        # Retries can use a different model without changing runtime.model. Keep
        # that request's choice local to its runtime, including parallel agents.
        original, request = getattr(runtime, "_web_models", (runtime.model, runtime.model))
        return request if original is runtime.model else runtime.model

    def key(self, model) -> tuple:
        credential = self.api.auth.all().get(model.provider, {})
        # Invalidate on account/key changes without retaining secrets in the cache.
        identity = json.dumps(credential, sort_keys=True, default=str)
        identity += str(model.options.get("apiKey") or os.environ.get(model.api_key_env or "", ""))
        return (model.provider, endpoint(model), model.api, model.id,
                hashlib.sha256(identity.encode()).hexdigest())

    def _permitted(self, runtime, model) -> bool:
        provider = self.api.services.get("execution")
        if provider is None or self.api.has_tool_hooks():
            return False
        request = next((m for m in reversed(runtime.messages) if isinstance(m, UserMessage) and m.kind is None), None)
        ev = ToolCallEvent(
            call_id=f"native-{self.operation}-{runtime.name}-{id(request)}", tool_name=f"web_{self.operation}",
            args={"native": True, "provider": model.provider, "request": request.text() if request else "",
             "scope": f"Allow the provider to perform public web {self.operation} operations for this user request."},
            tool=self.tool, cwd=runtime.cwd, agent=runtime.name, signal=runtime.agent.signal,
            origin=runtime.origin,
        )
        # Reevaluate the current deterministic policy for every model request.
        # If the provider's broad scope is not already authorized, expose the
        # client tool so the user can approve its actual query or URL.
        return provider.preflight(ev) is None

    def execution_backend(self, ctx):
        runtime = self._runtime(ctx)
        model = self._model_for(ctx)
        binding = getattr(runtime, "_web_models", None)
        backends = getattr(runtime, "_web_backends", {})
        config = getattr(runtime, "_web_backend_config", {})
        if binding and binding[0] is runtime.model and config.get(self.operation) == self.config and self.operation in backends:
            backend = backends[self.operation]
            if backend is None:
                raise ValueError(self.api.t('features.web.client_disabled', operation=self.operation))
            return backend
        return self.backend_for(model)

    def backend_for(self, model) -> str:
        if self.mode == "off":
            raise ValueError(self.api.t('features.web.disabled', operation=self.operation))
        if self.mode != "builtin" and model.options.get("native" + self.operation.title()) is not False:
            for offer in offers_for(model):
                if (offer.native and not offer.hosted and offer.capabilities == {self.operation}
                        and self.client_available(offer.backend, model)):
                    return offer.backend
        if self.mode == "native":
            raise ValueError(self.api.t('features.web.native_client', operation=self.operation))
        return self.config.get("backend", self.default_backend)
