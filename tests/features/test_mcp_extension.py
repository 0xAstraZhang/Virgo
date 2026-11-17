"""MCP is an extension built on the official SDK, not a built-in plugin."""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path

from virgo.agent import ToolContext
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage

from tests.support import SUPPORT
from tests.support.harness import calls

EXTENSION = Path(__file__).resolve().parents[2] / "extensions" / "mcp.py"
SERVER = SUPPORT / "mcp_echo_server.py"


def install_extension() -> None:
    extensions = Path(os.environ["VIRGO_HOME"]) / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXTENSION, extensions / "mcp.py")


def configure(path: Path, command: str = sys.executable, args: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echo": {
                        "command": command,
                        "args": args if args is not None else [str(SERVER)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_mcp_is_not_built_in(make_session, project):
    configure(project / ".virgo" / "mcp.json")
    session = make_session(
        settings={"plugins": {"mcp": {"enabled": True}}, "features": {"mcp": True}}
    )
    assert session.tool("mcp_status") is None
    assert not any(tool.name.startswith("mcp__") for tool in session.tools_for("main"))
    assert any("mcp extension" in notice for notice in session.settings.migration_warnings)


def test_mcp_extension_exposes_and_rebinds_tools(make_session, project):
    install_extension()
    config = project / ".virgo" / "mcp.json"
    configure(config)
    session = make_session(
        faux.reply(
            tools=[
                ("mcp__echo__echo", {"text": "hello mcp"}),
                ("mcp__echo__add", {"a": 1, "b": 2}),
            ]
        ),
        faux.reply("ok"),
    )
    session.prompt("use mcp")
    assert "mcp__echo__echo" in calls()[0]["tools"]
    results = [message for message in session.main.messages if isinstance(message, ToolResultMessage)]
    assert [message.text() for message in results] == ["hello mcp", "3"]
    assert session.main.agent.last_text == "ok"
    context = ToolContext("direct", project, threading.Event())
    direct = session.tool("mcp__echo__add").execute({"a": 20, "b": 30}, context)
    assert direct.as_text() == "50"
    status = session.tool("mcp_status").execute({"server": "echo"}, context).as_text()
    assert '"server": "echo"' in status and '"state": "ready"' in status
    assert session.tool("mcp_status").execute({"server": "missing"}, context).is_error

    # Removing the server removes its tools at the next publication.
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    session.prompt("/mcp reconnect")
    assert session.tool("mcp__echo__add") is None
    assert not any(tool.name.startswith("mcp__") for tool in session.tools_for("main"))

    configure(config)
    session.prompt("/mcp reconnect")
    assert session.tool("mcp__echo__echo") is not None


def test_mcp_extension_reports_unavailable_servers_without_stopping(make_session):
    install_extension()
    session = make_session(
        settings={"mcpServers": {"broken": {"command": "/nonexistent/mcp-server", "timeout": 5}}}
    )
    status = session.tool("mcp_status").execute(
        {}, ToolContext("status", session.cwd, threading.Event())
    ).as_text()
    assert '"server": "broken"' in status and '"state": "unavailable"' in status
    assert session.tool("mcp_status") is not None


def test_mcp_extension_skips_unsupported_schemas(make_session, project, tmp_path):
    install_extension()
    server = tmp_path / "schema_server.py"
    server.write_text(
        textwrap.dedent(
            """
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                if "id" not in request:
                    continue
                rid, method = request["id"], request.get("method")
                if method == "initialize":
                    result = {"protocolVersion": request["params"]["protocolVersion"],
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "schema", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [
                        {"name": "good", "description": "good",
                         "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "bad", "description": "bad",
                         "inputSchema": {"type": "object",
                                         "properties": {"x": {"type": 42}}}},
                        {"name": "constraint", "description": "constraint",
                         "inputSchema": {"type": "object",
                                         "properties": {"x": {"type": "string",
                                                              "minLength": "1"}}}},
                    ]}
                else:
                    result = {"content": [{"type": "text", "text": "ok"}]}
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    configure(project / ".virgo" / "mcp.json", args=[str(server)])
    session = make_session()
    assert session.tool("mcp__echo__good") is not None
    assert session.tool("mcp__echo__bad") is None
    assert session.tool("mcp__echo__constraint") is None


def test_mcp_extension_reports_terminated_connections(make_session, project, tmp_path):
    install_extension()
    server = tmp_path / "exit_server.py"
    server.write_text(
        textwrap.dedent(
            """
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                if "id" not in request:
                    continue
                rid, method = request["id"], request.get("method")
                if method == "initialize":
                    result = {"protocolVersion": request["params"]["protocolVersion"],
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "exit", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "echo", "description": "echo",
                                         "inputSchema": {"type": "object", "properties": {}}}]}
                else:
                    result = {"content": [{"type": "text", "text": "ok"}]}
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
                if method == "tools/list":
                    sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    configure(project / ".virgo" / "mcp.json", args=[str(server)])
    session = make_session()
    context = ToolContext("status", project, threading.Event())
    assert session.tool("mcp__echo__echo") is not None
    time.sleep(0.5)  # Let the server process exit before touching the transport.
    assert session.tool("mcp__echo__echo").execute({}, context).is_error
    assert '"state": "unavailable"' in session.tool("mcp_status").execute({}, context).as_text()


def test_mcp_extension_ignores_malformed_config(make_session, project):
    install_extension()
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text("[]", encoding="utf-8")
    session = make_session()
    assert "mcp: invalid" in session.ui.text
    assert session.tool("mcp_status") is not None
