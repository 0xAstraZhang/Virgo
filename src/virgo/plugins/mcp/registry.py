"""MCP connection registry: generation leases and boundary publication."""

from __future__ import annotations

import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field

from ...agent.types import AgentTool, ToolContext, ToolResult
from ...diagnostics import safe_text
from ...harness.extensions import ROLES_ALL, ExtensionAPI
from .client import (
    MAX_RECONNECT_ATTEMPTS,
    MCPClient,
    MCPError,
    _schemas,
    to_result,
    tool_name,
)


@dataclass
class _Connection:
    name: str
    config: dict
    client: MCPClient
    generation: int = 1
    inflight: int = 0
    reconnecting: bool = False
    retired: bool = False
    attempts_without_progress: int = 0
    pending: MCPClient | None = None
    pending_context: ToolContext | None = None
    recovery_error: dict | None = None
    registered: set[str] = field(default_factory=set)
    wrappers: dict[str, AgentTool] = field(default_factory=dict)
    schemas: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class MCPConnections:
    """Generation-bound dispatch handles, with bounded, explicit recovery.

    Reconnection never replays a tools/call. Recovered clients are published only
    by refresh(), which the harness calls between completed tool batches and the
    next model request. An active response keeps its old schema and transport;
    stale direct handles are rejected once their owning response has ended.
    """

    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.clients: dict[str, MCPClient] = {}
        self.connections: dict[str, _Connection] = {}
        self._registry_lock = threading.RLock()
        self._revision = 0
        self._seen = weakref.WeakKeyDictionary()
        self._generations: dict[str, int] = {}
        self._retired: list[_Connection] = []
        self._closed = False

    @staticmethod
    def _holds_snapshot(slot: _Connection, runtime, remote: str | None = None) -> bool:
        return runtime is not None and runtime.agent.running and any(
            slot.wrappers.get(tool.name) is tool
            and (remote is None or tool.name == tool_name(slot.name, remote))
            for tool in runtime.tools
        )

    def _retire(self, slot: _Connection) -> None:
        """Called under registry/slot locks after a replacement is published."""
        slot.retired = True
        slot.pending = slot.pending_context = None
        self._retired.append(slot)

    def reap(self, *, boundary=None) -> None:
        """Retain only old generations still used by another active response."""
        closing = []
        with self._registry_lock:
            for slot in tuple(self._retired):
                with slot.lock:
                    if slot.inflight or any(
                        runtime is not boundary and self._holds_snapshot(slot, runtime)
                        for runtime in self._seen
                    ):
                        continue
                    self._retired.remove(slot)
                    # Revoke the lease before closing outside the lock. A stale
                    # direct handle cannot borrow a runtime's later active turn.
                    slot.wrappers.clear()
                    closing.append(slot.client)
        for client in closing:
            client.close()

    def on_agent_event(self, event, runtime) -> None:
        if event.type in {"turn_end", "agent_end"}:
            # These events also cover refusal, error and cancellation without a
            # later model request. No tool from this response can run afterward.
            self.reap(boundary=runtime)

    def _client(self, name: str, conf: dict, generation: int = 1, *, cwd=None) -> MCPClient:
        client = MCPClient(
            name, conf["command"], conf.get("args"), conf.get("env"), cwd or self.api.cwd
        )
        client.generation = generation
        return client

    def _report(self, client: MCPClient, error, ctx: ToolContext, *, phase=None, context=None) -> dict:
        runtime = self.api.runtime_for_context(ctx)
        owner = (
            (runtime.store if runtime.store is not None else runtime.accounting_store)
            if runtime is not None
            else self.api.current_store()
        )
        metadata = {
            **(runtime.diagnostic_context() if runtime is not None else {}),
            "agent": ctx.agent,
            "tool_call_id": ctx.call_id,
            **(context or {}),
        }
        details = client.diagnostic(error, phase=phase, context=metadata)
        details = self.api.report_error(details, phase=details["phase"], store=owner, notify=False)
        # Direct extension callers need the same scope as the normal loop.
        access = getattr(runtime, "diagnostic_access", None)
        if access is not None:
            access.remember(details)
        return details

    @staticmethod
    def _blocked(slot: _Connection, reason: str) -> ToolResult:
        return ToolResult.error(
            reason,
            details={
                "mcp": True,
                "transport": {
                    "protocol": "mcp",
                    "server": slot.name,
                    "generation": slot.generation,
                    "dispatch": "not_started",
                    "outcome": "not_started",
                    "side_effects": "none",
                },
            },
        )

    def _execute(
        self, slot: _Connection, remote: str, schema: str, args: dict, ctx: ToolContext
    ) -> ToolResult:
        with slot.lock:
            if self._closed:
                return self._blocked(slot, ctx.t("features.mcp.retired"))
            runtime = self.api.runtime_for_context(ctx)
            leased = self._holds_snapshot(slot, runtime, remote)
            if slot.retired and not leased:
                return self._blocked(slot, ctx.t("features.mcp.schema_changed"))
            if (slot.reconnecting or slot.pending is not None) and not leased:
                return self._blocked(slot, ctx.t("features.mcp.recovering"))
            if slot.schemas.get(remote) != schema:
                return self._blocked(slot, ctx.t("features.mcp.schema_changed"))
            client = slot.client
            slot.inflight += 1
        try:
            metadata = {
                "server": slot.name,
                "method": "tools/call",
                "mcp_tool": remote,
                "tool_name": tool_name(slot.name, remote),
                "generation": client.generation,
            }
            try:
                result = to_result(client.call(remote, args, signal=ctx.signal))
                if result.is_error:
                    metadata["request_id"] = getattr(client._request_local, "id", None)
                    result.details["error"] = self._report(
                        client,
                        "MCP server reported a tool error.",
                        ctx,
                        phase="mcp.tool",
                        context=metadata,
                    )
                else:
                    with slot.lock:
                        slot.attempts_without_progress = 0
                        slot.recovery_error = None
            except Exception as error:
                details = self._report(
                    client, error, ctx, phase=getattr(error, "phase", "mcp.tool"), context=metadata
                )
                result = ToolResult.error(
                    safe_text(error, secrets=client._secrets), {"mcp": True, "error": details}
                )
            result.details["transport"] = client.transport()
            if result.is_error:
                with slot.lock:
                    client.error_details = result.details["error"]
            return result
        finally:
            with slot.lock:
                slot.inflight -= 1

    def _prepare_tools(self, slot: _Connection, client: MCPClient, schemas: dict) -> list[AgentTool]:
        seen = {
            tool.name
            for role in ROLES_ALL
            for tool in self.api.tools_for(role)
            if slot.wrappers.get(tool.name) is not tool
        }
        prepared = []
        for spec in client.tools:
            name = tool_name(slot.name, spec["name"])
            if name in seen:
                raise MCPError(
                    f"{slot.name}: tool name {name} conflicts with an active tool",
                    phase="mcp.schema",
                    method="tools/list",
                    dispatch="response_received",
                    outcome="unknown",
                )
            schema = deepcopy(spec.get("inputSchema") or {"type": "object", "properties": {}})

            def execute(args, ctx, _slot=slot, _tool=spec["name"], _schema=schemas[spec["name"]]):
                return self._execute(_slot, _tool, _schema, args, ctx)

            desc = (spec.get("description") or spec["name"]).strip()
            prepared.append(AgentTool(name, f"[MCP {slot.name}] {desc}", schema, execute))
            seen.add(name)
        return prepared

    def _publish(
        self, slot: _Connection, client: MCPClient, schemas: dict, prepared: list[AgentTool]
    ) -> None:
        # Publish only our owned namespace. The harness takes an immutable tool
        # snapshot for each request/batch; no plugin writes its registry directly.
        tools = [
            tool for name, connection in self.connections.items() if name != slot.name
            for tool in connection.wrappers.values()
        ]
        self.api.replace_tools("servers", [(tool, ROLES_ALL) for tool in [*tools, *prepared]])
        slot.wrappers = {tool.name: tool for tool in prepared}
        slot.registered = set(slot.wrappers)
        slot.client = client
        slot.generation = client.generation
        slot.schemas = schemas
        self.connections[slot.name] = slot
        self.clients[slot.name] = client
        self._generations[slot.name] = client.generation

    def connect_all(self, config: dict) -> None:
        owner = self.api.current_store()

        def one(item):
            name, conf = item
            client = self._client(name, conf, self._generations.get(name, 0) + 1)
            schemas = {}
            try:
                client.connect(timeout=float(conf.get("timeout", 30)))
                schemas = _schemas(client)
            except Exception as error:
                client.error = safe_text(error, secrets=client._secrets) or type(error).__name__
                client.close()
                client.error_details = client.diagnostic(
                    error,
                    phase=getattr(error, "phase", "mcp.connect" if client.proc else "mcp.start"),
                )
            return name, conf, client, schemas

        if config:
            with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
                for name, conf, client, schemas in pool.map(one, config.items()):
                    slot = _Connection(
                        name, deepcopy(conf), client, generation=client.generation, schemas=schemas
                    )
                    self.connections[name] = slot
                    self.clients[name] = client
                    self._generations[name] = client.generation
                    if client.error:
                        client.error_details = self.api.report_error(
                            client.error_details,
                            phase="mcp.connect",
                            store=owner,
                        )
                    else:
                        try:
                            prepared = self._prepare_tools(slot, client, schemas)
                            self._publish(slot, client, schemas, prepared)
                        except Exception as error:
                            client.error = safe_text(error, secrets=client._secrets)
                            client.close()
                            client.error_details = self.api.report_error(
                                client.diagnostic(error, phase="mcp.schema"),
                                phase="mcp.schema",
                                store=owner,
                            )

    def status(self, args: dict, ctx: ToolContext) -> ToolResult:
        server = args.get("server")
        runtime = self.api.runtime_for_context(ctx)
        if runtime is None:
            return ToolResult.error(ctx.t("features.mcp.status_runtime"))
        access = getattr(runtime, "diagnostic_access", None)
        with self._registry_lock:
            slots = (
                ([self.connections[server]] if server in self.connections else [])
                if server
                else list(self.connections.values())
            )
        if server and not slots:
            return ToolResult.error(ctx.t("features.mcp.no_server"))
        rows = []
        for slot in slots:
            with slot.lock:
                client = slot.client
                ready = not client.error and not client._reader_done.is_set() and not client._closing
                row = {
                    "server": slot.name,
                    "generation": slot.generation,
                    "state": "reconnecting"
                    if slot.reconnecting
                    else "refresh_required"
                    if slot.pending
                    else "ready"
                    if ready
                    else "unavailable",
                    "inflight": slot.inflight,
                    "tools": len(slot.schemas),
                    "reconnect_attempts_remaining": max(
                        0, MAX_RECONNECT_ATTEMPTS - slot.attempts_without_progress
                    ),
                }
                error = slot.recovery_error or client.error_details
                visible = access.get(error["error_id"]) if access is not None and error else None
                if visible:
                    row["error_id"] = visible["error_id"]
                rows.append(row)
        return ToolResult.text(
            json.dumps({"servers": rows}, ensure_ascii=False), details={"mcp_status": rows}
        )

    def reconnect(self, args: dict, ctx: ToolContext) -> ToolResult:
        runtime = self.api.runtime_for_context(ctx)
        if runtime is None:
            return ToolResult.error(ctx.t("features.mcp.recovery_runtime"))
        access = getattr(runtime, "diagnostic_access", None)
        error = access.get(args["error_id"]) if access is not None else None
        if error is None:
            return ToolResult.error(ctx.t("features.mcp.evidence_missing"))
        server = args["server"]
        context = error.get("context", {})
        if context.get("server") != server or not str(error.get("phase", "")).startswith("mcp."):
            return ToolResult.error(ctx.t("features.mcp.evidence_mismatch"))
        with self._registry_lock:
            slot = self.connections.get(server)
        if slot is None:
            return ToolResult.error(ctx.t("features.mcp.no_server"))
        with slot.lock:
            if slot.retired or context.get("generation") != slot.generation:
                return self._blocked(slot, ctx.t("features.mcp.old_connection"))
            if slot.reconnecting or slot.pending is not None:
                return self._blocked(slot, ctx.t("features.mcp.pending"))
            if slot.inflight:
                return self._blocked(slot, ctx.t("features.mcp.inflight"))
            if ctx.signal.is_set():
                return self._blocked(slot, ctx.t("features.mcp.cancelled"))
            if slot.attempts_without_progress >= MAX_RECONNECT_ATTEMPTS:
                return self._blocked(slot, ctx.t("features.mcp.budget"))
            slot.reconnecting = True
            slot.attempts_without_progress += 1
            generation = slot.generation
            conf = slot.config
        candidate = None
        try:
            candidate = self._client(server, conf, generation + 1, cwd=slot.client.cwd)
            candidate.connect(timeout=float(conf.get("timeout", 30)))
            schemas = _schemas(candidate)
            # Build every closure and check active-name conflicts before an old
            # connection or registered tool can be replaced.
            with self._registry_lock:
                self._prepare_tools(slot, candidate, schemas)
            if ctx.signal.is_set():
                candidate.close()
                return self._blocked(slot, ctx.t("features.mcp.cancelled_retained"))
            with slot.lock:
                if slot.retired:
                    candidate.close()
                    return self._blocked(slot, ctx.t("features.mcp.removed"))
                # Zero active calls does not mean zero response leases: a model
                # may still be generating, or a batch may be between calls.
                # Publish a new slot at the next boundary for every recovery.
                slot.pending = candidate
                slot.pending_context = ctx
                slot.recovery_error = None
            state = {
                "server": server,
                "generation": candidate.generation,
                "state": "refresh_required",
                "refresh_required": True,
                "replayed_calls": 0,
            }
            return ToolResult.text(
                json.dumps(state) + ctx.t("features.mcp.no_replay"),
                details={"mcp_recovery": state},
            )
        except Exception as failure:
            if candidate is not None:
                candidate.close()
            diagnostic_client = candidate or slot.client
            transport = (
                candidate.transport()
                if candidate is not None
                else {
                    "protocol": "mcp",
                    "server": server,
                    "generation": generation + 1,
                    "dispatch": "not_started",
                    "outcome": "not_started",
                    "side_effects": "none",
                }
            )
            if isinstance(failure, MCPError):
                transport.update(
                    dispatch=failure.dispatch,
                    outcome=failure.outcome,
                    side_effects="none" if failure.dispatch == "not_started" else "unknown",
                )
            details = self._report(
                diagnostic_client,
                failure,
                ctx,
                phase=getattr(failure, "phase", "mcp.reconnect"),
                context={
                    **transport,
                    "server": server,
                    "generation": generation,
                    "candidate_generation": generation + 1,
                    "tool_name": "mcp_reconnect",
                    "related_error_id": error["error_id"],
                },
            )
            with slot.lock:
                slot.recovery_error = details
            return ToolResult.error(
                ctx.t("features.mcp.failed"),
                details={
                    "mcp": True,
                    "error": details,
                    "transport": transport,
                },
            )
        finally:
            with slot.lock:
                slot.reconnecting = False

    def refresh(self, runtime) -> bool:
        """Publish pending clients at a safe boundary; the harness refreshes its own runtime."""
        old_clients = []
        with self._registry_lock:
            for slot in list(self.connections.values()):
                with slot.lock:
                    candidate = slot.pending
                    if candidate is None or slot.reconnecting or slot.inflight or slot.retired:
                        continue
                    try:
                        schemas = _schemas(candidate)
                        replacement = _Connection(
                            slot.name, deepcopy(slot.config), candidate, generation=candidate.generation,
                            schemas=schemas, wrappers=dict(slot.wrappers),
                            attempts_without_progress=slot.attempts_without_progress,
                        )
                        prepared = self._prepare_tools(replacement, candidate, schemas)
                        self._publish(replacement, candidate, schemas, prepared)
                    except Exception as error:
                        ctx = slot.pending_context
                        slot.pending = slot.pending_context = None
                        old_clients.append(candidate)
                        if ctx is not None:
                            details = self._report(
                                candidate,
                                error,
                                ctx,
                                phase="mcp.schema",
                                context={
                                    "server": slot.name,
                                    "generation": slot.generation,
                                    "candidate_generation": candidate.generation,
                                    "tool_name": "mcp_reconnect",
                                },
                            )
                            slot.recovery_error = details
                            owner = self.api.runtime_for_context(ctx)
                            if owner is not None:
                                owner.recovery.safely("queue", details)
                        continue
                    self._retire(slot)
                    self._revision += 1
            changed = self._seen.get(runtime, 0) != self._revision
            self._seen[runtime] = self._revision
        for old in old_clients:
            old.close()
        self.reap(boundary=runtime)
        return changed

    def reload(self, config: dict) -> None:
        """Validate all replacements before publishing or closing any healthy connection."""
        with self._registry_lock:
            slots = list(self.connections.values())
            candidates = []
            replacements = {}
            locked = []
            try:
                for name, conf in config.items():
                    client = self._client(name, conf, self._generations.get(name, 0) + 1)
                    candidates.append(client)
                    client.connect(timeout=float(conf.get("timeout", 30)))
                    schemas = _schemas(client)
                    previous = self.connections.get(name)
                    slot = _Connection(name, deepcopy(conf), client, generation=client.generation,
                                       schemas=schemas,
                                       wrappers=dict(previous.wrappers) if previous is not None else {})
                    prepared = self._prepare_tools(slot, client, schemas)
                    slot.wrappers = {tool.name: tool for tool in prepared}
                    slot.registered = set(slot.wrappers)
                    replacements[name] = slot
                # A child may have started a call during the handshakes. Reserve
                # all old handles and fail without changing anything if so.
                for slot in slots:
                    slot.lock.acquire()
                    locked.append(slot)
                if any(slot.inflight or slot.reconnecting or slot.pending is not None for slot in slots):
                    raise ValueError(self.api.t("features.mcp.inflight"))
                self.api.replace_tools("servers", [
                    (tool, ROLES_ALL) for slot in replacements.values() for tool in slot.wrappers.values()
                ])
                for slot in slots:
                    self._retire(slot)
                self.connections = replacements
                self.clients.clear()
                self.clients.update({name: slot.client for name, slot in replacements.items()})
                self._generations.update({name: slot.generation for name, slot in replacements.items()})
                self._revision += 1
            except BaseException:
                for client in candidates:
                    client.close()
                raise
            finally:
                for slot in reversed(locked):
                    slot.lock.release()
        self.reap()

    def close(self) -> None:
        closing = []
        with self._registry_lock:
            self._closed = True
            for slot in [*self.connections.values(), *self._retired]:
                with slot.lock:
                    slot.retired = True
                    closing.extend(client for client in (slot.client, slot.pending) if client is not None)
            self._retired.clear()
        for client in closing:
            client.close()

