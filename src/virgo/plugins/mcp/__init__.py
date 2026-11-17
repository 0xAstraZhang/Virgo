"""MCP: connect stdio Model Context Protocol servers and expose their tools as ``mcp__<server>__<tool>``.

Configure in ``.virgo/mcp.json`` (or ``~/.virgo/mcp.json``, or settings ``mcpServers``) using the
common format::

    {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

Servers are connected at session start (in parallel). Recovered connections enter
runtime snapshots at a safe boundary, even when their schemas are unchanged.
MCP calls and reconnection need permission by default; allow calls with rules like
``mcp__github__*``. Reconnection never replays a failed call.
"""

from __future__ import annotations

import json

from ...agent.types import AgentTool
from ...diagnostics import safe_text
from ...harness.extensions import ROLES_ALL, ExtensionAPI
from .client import (
    MAX_RECONNECT_ATTEMPTS,
    MAX_TOOL_PAGES,
    PROTOCOL_VERSION,
    STDERR_LIMIT,
    MCPClient,
    MCPError,
    to_result,
    tool_name,
)
from .registry import MCPConnections

__all__ = [
    "MAX_RECONNECT_ATTEMPTS",
    "MAX_TOOL_PAGES",
    "MCPClient",
    "MCPConnections",
    "MCPError",
    "PROTOCOL_VERSION",
    "STDERR_LIMIT",
    "load_config",
    "setup",
    "to_result",
    "tool_name",
]


def load_config(api: ExtensionAPI) -> dict:
    servers: dict = {}
    for f in api.paths.both("mcp.json"):
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                listed = data.get("mcpServers", {}) if isinstance(data, dict) else None
                if not isinstance(listed, dict):
                    raise ValueError("mcpServers must be an object")
                servers.update(listed)
            except ValueError as e:  # includes JSONDecodeError
                api.report_error(e, phase="mcp.config", context={"path": str(f)}, notify=False)
                api.ui.print(
                    api.t("features.mcp.invalid_config", path=f, error=safe_text(e)), "error"
                )
    from_settings = api.settings.get("mcpServers", {}) or {}
    if isinstance(from_settings, dict):
        servers.update(from_settings)
    else:
        api.report_error("mcpServers must be an object", phase="mcp.config", notify=False)
        api.ui.print(api.t("features.mcp.invalid_settings"), "error")
    return {
        k: v for k, v in servers.items() if isinstance(v, dict) and v.get("command") and not v.get("disabled")
    }


def setup(api: ExtensionAPI) -> None:
    manager = MCPConnections(api)
    api.services["mcp"] = manager.clients
    api.on_cleanup(manager.close)
    api.on("runtime_request", manager.refresh)
    api.on("agent_event", manager.on_agent_event)
    manager.connect_all(load_config(api))
    api.register_tool(
        AgentTool(
            "mcp_status",
            "Read local MCP connection state, generation, pending calls and accessible diagnostic IDs. Does not contact servers.",
            {"type": "object", "properties": {"server": {"type": "string"}}, "additionalProperties": False},
            manager.status,
            side_effect="read",
            i18n_key="tools.mcp_status",
        ),
        roles=ROLES_ALL,
    )
    api.register_tool(
        AgentTool(
            "mcp_reconnect",
            "Reconnect one configured MCP server using an accessible diagnostic ID. Starts its configured process; never replays failed calls. "
            "Only use after checking the failure and previous operation's outcome; in-flight calls and repeated recovery without progress block this action.",
            {
                "type": "object",
                "properties": {"server": {"type": "string"}, "error_id": {"type": "string"}},
                "required": ["server", "error_id"],
                "additionalProperties": False,
            },
            manager.reconnect,
            i18n_key="tools.mcp_reconnect",
        ),
        roles=ROLES_ALL,
    )

    def cmd(_session, args):
        if args.strip() == "reconnect":
            manager.reload(load_config(api))
            api.ui.print(api.t("features.mcp.reconnected"))
        if not manager.clients:
            api.ui.print(api.t("features.mcp.empty"))
            return
        for client in manager.clients.values():
            state = (
                api.t("features.mcp.error", error=client.error)
                if client.error
                else api.t("features.mcp.tools", count=len(client.tools))
            )
            api.ui.print(f"  {client.name}: {state}")

    api.register_command(
        "mcp",
        cmd,
        "show MCP servers (/mcp reconnect)",
        argument_hint="[reconnect]",
        description_key="features.commands.mcp",
    )

