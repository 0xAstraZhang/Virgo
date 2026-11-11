"""MCP: connect stdio Model Context Protocol servers and expose their tools as ``mcp__<server>__<tool>``.

Configure in ``.virgo/mcp.json`` (or ``~/.virgo/mcp.json``, or settings ``mcpServers``) using the
common format::

    {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

Servers are connected at session start (in parallel); their tools are part of the frozen tool set.
MCP tools need permission by default; allow them with rules like ``mcp__github__*``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import __version__
from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import ImageContent, TextContent
from ..harness.extensions import ROLES_ALL, ExtensionAPI

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


class MCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
        cwd: Path | None = None,
    ):
        self.name = name
        self.command = command
        self.args = [_expand(str(a)) for a in args or []]
        self.env = {**os.environ, **{k: _expand(str(v)) for k, v in (env or {}).items()}}
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.server_info: dict = {}
        self.error: str | None = None
        self._next = 0
        self._pending: dict[int, dict] = {}
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()

    # -- transport -----------------------------------------------------------
    def start(self) -> None:
        self.proc = subprocess.Popen(
            [_expand(self.command), *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.env,
            cwd=str(self.cwd) if self.cwd else None,
        )
        threading.Thread(target=self._reader, daemon=True, name=f"mcp-{self.name}").start()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._cv:
                    self._pending[msg["id"]] = msg
                    self._cv.notify_all()
            elif "id" in msg and "method" in msg:  # server -> client request: we support none
                self._send(
                    {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}}
                )
        with self._cv:
            self.error = self.error or "server exited"
            self._cv.notify_all()

    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        with self._write_lock:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        with self._cv:
            self._next += 1
            rid = self._next
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"{self.name}: cannot write to server ({e})") from e
        with self._cv:
            ok = self._cv.wait_for(lambda: rid in self._pending or self.proc.poll() is not None, timeout)
            msg = self._pending.pop(rid, None)
        if msg is None:
            raise MCPError(
                f"{self.name}: {'timeout' if not ok else self.error or 'no response'} for {method}"
            )
        if "error" in msg:
            error = msg["error"]
            raise MCPError(f"{self.name}: {error.get('message', error) if isinstance(error, dict) else error}")
        if not isinstance(msg["result"], dict):
            raise MCPError(f"{self.name}: invalid result for {method}")
        return msg["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- protocol ------------------------------------------------------------
    def connect(self, timeout: float = 30.0) -> None:
        self.start()
        res = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "virgo", "version": __version__},
            },
            timeout,
        )
        self.server_info = res.get("serverInfo", {})
        self.notify("notifications/initialized")
        tools, cursor = [], None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            listed = page.get("tools", [])
            if not isinstance(listed, list):
                raise MCPError(f"{self.name}: invalid tools/list result")
            tools.extend(t for t in listed if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def call(self, tool: str, arguments: dict, timeout: float = 300.0) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def tool_name(server: str, tool: str) -> str:
    return f"mcp__{_norm(server)}__{_norm(tool)}"[:64]


def to_result(res: dict) -> ToolResult:
    content: list = []
    for c in res.get("content", []):
        if c.get("type") == "text":
            content.append(TextContent(c.get("text", "")))
        elif c.get("type") == "image":
            content.append(ImageContent(c.get("data", ""), c.get("mimeType", "image/png")))
        elif c.get("type") == "resource":
            r = c.get("resource", {})
            content.append(TextContent(r.get("text") or f"[resource {r.get('uri', '')}]"))
        else:
            content.append(TextContent(json.dumps(c)))
    if not content and "structuredContent" in res:
        content.append(TextContent(json.dumps(res["structuredContent"], ensure_ascii=False)))
    return ToolResult(
        content or [TextContent("(no content)")], details={"mcp": True}, is_error=bool(res.get("isError"))
    )


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
                api.ui.print(f"[mcp: invalid {f}: {e}]", "error")
    from_settings = api.settings.get("mcpServers", {}) or {}
    if isinstance(from_settings, dict):
        servers.update(from_settings)
    else:
        api.ui.print("[mcp: invalid settings: mcpServers must be an object]", "error")
    return {
        k: v for k, v in servers.items() if isinstance(v, dict) and v.get("command") and not v.get("disabled")
    }


def setup(api: ExtensionAPI) -> None:
    clients: dict[str, MCPClient] = {}
    api.services["mcp"] = clients
    registered: set[str] = set()  # tool names owned by the current connections

    def register(client: MCPClient) -> None:
        seen = {t.name for t in api.session.tools_for("main")}
        for spec in client.tools:
            name = tool_name(client.name, spec["name"])
            if name in seen:
                api.ui.print(f"[mcp: skipping duplicate tool name {name}]", "warn")
                continue
            schema = spec.get("inputSchema") or {"type": "object", "properties": {}}

            def execute(args: dict, ctx: ToolContext, _c=client, _t=spec["name"]) -> ToolResult:
                try:
                    return to_result(_c.call(_t, args))
                except MCPError as e:
                    return ToolResult.error(str(e))

            desc = (spec.get("description") or spec["name"]).strip()
            api.register_tool(
                AgentTool(
                    name,
                    f"[MCP {client.name}] {desc}",
                    schema,
                    execute,
                    snippet=f"[MCP {client.name}] {desc.splitlines()[0][:80]}",
                ),
                roles=ROLES_ALL,
            )
            seen.add(name)
            registered.add(name)

    def connect_all(config: dict) -> None:
        def one(item):
            name, conf = item
            c = MCPClient(name, conf["command"], conf.get("args"), conf.get("env"), api.session.cwd)
            try:
                c.connect(timeout=float(conf.get("timeout", 30)))
            except Exception as e:  # noqa: BLE001 - one broken server must not stop the session
                c.error = str(e) or type(e).__name__
                c.close()
            return c

        if not config:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(config))) as pool:
            for c in pool.map(one, config.items()):
                clients[c.name] = c
                if c.error:
                    api.ui.print(f"[mcp: {c.name} failed to connect: {c.error}]", "warn")
                else:
                    register(c)

    connect_all(load_config(api))

    def cmd(session, args):
        if args.strip() == "reconnect":
            for c in clients.values():
                c.close()
            clients.clear()
            # The old tools are bound to the clients just closed; keeping them would both shadow
            # the new connection's tools as duplicates and fail on every call.
            for name in registered:
                session.unregister_tool(name)
            registered.clear()
            connect_all(load_config(api))
            session.main.tools[:] = session.tools_for("main")
            session.main.rebuild_system()
            api.ui.print("MCP servers reconnected (tool set changed: the prompt cache restarts).")
        if not clients:
            api.ui.print("No MCP servers configured (.virgo/mcp.json).")
            return
        for c in clients.values():
            state = f"error: {c.error}" if c.error else f"{len(c.tools)} tools"
            api.ui.print(f"  {c.name}: {state}")

    api.register_command("mcp", cmd, "show MCP servers (/mcp reconnect)")
    api.on("session_shutdown", lambda s: [c.close() for c in clients.values()])
