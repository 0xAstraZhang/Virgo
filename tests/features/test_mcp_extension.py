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
from virgo.harness.extensions import ExtensionAPI

from tests.support import SUPPORT
from tests.support.harness import calls, wait_until

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
    context = ToolContext("direct", project, threading.Event(), origin=session.main.origin)
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
                    print("server stopped: " + sys.argv[-1], file=sys.stderr, flush=True)
                    sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    secret = "private-mcp-test-credential"
    configure(project / ".virgo" / "mcp.json", args=[str(server), "--api-key", secret])
    session = make_session()
    context = ToolContext("status", project, threading.Event(), origin=session.main.origin)
    assert session.tool("mcp__echo__echo") is not None
    time.sleep(0.5)  # Let the server process exit before touching the transport.
    result = session.tool("mcp__echo__echo").execute({}, context)
    assert result.is_error and "server stopped:" in result.as_text()
    assert secret not in result.as_text()
    assert '"state": "unavailable"' in session.tool("mcp_status").execute({}, context).as_text()


def test_mcp_extension_ignores_malformed_config(make_session, project):
    install_extension()
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text("[]", encoding="utf-8")
    session = make_session()
    assert "mcp: invalid" in session.ui.text
    assert session.tool("mcp_status") is not None


def test_mcp_project_configuration_requires_explicit_trust(make_session, project):
    install_extension()
    initialized = project / "untrusted-server-started.json"
    configure(project / ".virgo" / "mcp.json", args=[str(SERVER), str(initialized)])
    session = make_session(trust_project_extensions=False)
    assert not initialized.exists()
    assert session.tool("mcp__echo__echo") is None
    assert "project MCP configuration" in session.ui.text


def test_mcp_process_call_requires_its_origin(make_session, project):
    install_extension()
    configure(project / ".virgo" / "mcp.json")
    session = make_session()
    result = session.tool("mcp__echo__echo").execute(
        {"text": "unbound"}, ToolContext("unbound", project, threading.Event())
    )
    assert result.is_error
    assert "originating runtime" in result.as_text()


def test_mcp_connections_follow_runtime_and_filter_ambient_credentials(make_session, project, monkeypatch):
    install_extension()
    server = SUPPORT / "mcp_boundary_server.py"
    if sys.platform == "linux":
        # Match bubblewrap's real parent-thread lifetime requirement without
        # changing this Host fixture into a simulated native sandbox.
        source = server.read_text(encoding="utf-8")
        server = project / "thread_owned_mcp_server.py"
        server.write_text(
            "import ctypes, signal\n"
            "assert ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) == 0\n"
            + source,
            encoding="utf-8",
        )
    monkeypatch.setenv("MCP_AMBIENT_SECRET", "must-not-inherit")
    monkeypatch.setenv("MCP_APPROVED_SECRET", "approved")
    session = make_session(settings={"mcpServers": {"boundary": {
        "command": sys.executable, "args": [str(server)],
        "env": {"MCP_TEST_CREDENTIAL": "${MCP_APPROVED_SECRET}"},
    }}})
    api = ExtensionAPI(session, "mcp-origin-acceptance")
    child = api.create_runtime("mcp-child", "subagent", parent=session.main)
    tool = session.tool("mcp__boundary__probe")
    try:
        main_context = ToolContext("main", project, threading.Event(), origin=session.main.origin)
        child_context = ToolContext("child", project, threading.Event(), agent=child.name, origin=child.origin)
        main = json.loads(tool.execute({}, main_context).as_text())
        other = json.loads(tool.execute({}, child_context).as_text())
        again = json.loads(tool.execute({}, main_context).as_text())
        assert main["pid"] == again["pid"] != other["pid"]
        assert main["credential"] == other["credential"] == "approved"
        assert main["ambient_secret"] is other["ambient_secret"] is None
        assert main["cwd"] == other["cwd"] == str(project)
        assert tool.execution_scope == "process"
    finally:
        api.release_runtime(child)


def test_mcp_cancellation_drains_server_tree_without_replay(make_session, project):
    install_extension()
    configure(project / ".virgo" / "mcp.json", args=[str(SUPPORT / "mcp_boundary_server.py")])
    session = make_session()
    tool = session.tool("mcp__echo__probe")
    signal = threading.Event()
    context = ToolContext("cancel", project, signal, origin=session.main.origin)
    ready, late = project / "mcp-ready", project / "mcp-late"
    results = []
    worker = threading.Thread(target=lambda: results.append(tool.execute(
        {"ready": str(ready), "child_marker": str(late), "wait": 15}, context)))
    worker.start()
    try:
        wait_until(ready.exists, timeout=5)
        signal.set()
        worker.join(10)
        assert not worker.is_alive()
        assert results[0].is_error and "cancelled" in results[0].as_text()
        signal.clear()
        assert tool.execute({}, context).is_error  # No implicit replacement process.
        time.sleep(2.2)
        assert not late.exists()
    finally:
        signal.set()
        worker.join(10)


def test_mcp_validates_config_releases_closed_connections_and_handles_shared_home(make_session, project, monkeypatch):
    install_extension()
    session = make_session(settings={"mcpServers": {
        "bad_args": {"command": sys.executable, "args": "--not-a-list"},
        "bad_env": {"command": sys.executable, "args": [str(SERVER)], "env": ["NOT=DICT"]},
        "bad_timeout": {"command": sys.executable, "args": [str(SERVER)], "timeout": True},
        "good": {"command": sys.executable, "args": [str(SERVER)]},
    }})
    context = ToolContext("status", project, threading.Event(), origin=session.main.origin)
    rows = {row["server"]: row for row in json.loads(session.tool("mcp_status").execute({}, context).as_text())["servers"]}
    assert rows["bad_args"]["state"] == "unavailable" and "args must be a list" in rows["bad_args"]["error"]
    assert rows["bad_env"]["state"] == "unavailable" and "env must be an object" in rows["bad_env"]["error"]
    assert rows["bad_timeout"]["state"] == "unavailable" and "timeout" in rows["bad_timeout"]["error"]
    assert rows["good"]["state"] == "ready"

    manager = session.tool("mcp_status").execute.__self__
    assert len(manager._owned) == 1
    api = ExtensionAPI(session, "mcp-owned-check")
    child = api.create_runtime("mcp-ephemeral", "subagent", parent=session.main)
    try:
        child_ctx = ToolContext("child", project, threading.Event(), agent=child.name, origin=child.origin)
        assert session.tool("mcp__good__echo").execute({"text": "hi"}, child_ctx).as_text() == "hi"
        assert len(manager._owned) == 2
    finally:
        api.release_runtime(child)
    assert len(manager._owned) == 1

    # When VIRGO_HOME resolves to <project>/.virgo (even through a symlink),
    # user mcp.json and user extensions load once without an untrusted-project warning.
    shared_dir = project / ".virgo"
    configure(shared_dir / "mcp.json")
    (shared_dir / "extensions").mkdir(parents=True, exist_ok=True)
    shutil.copy(EXTENSION, shared_dir / "extensions" / "mcp.py")
    if os.name == "posix":
        alias = project.parent / "virgo-home-link"
        alias.symlink_to(shared_dir)
        monkeypatch.setenv("VIRGO_HOME", str(alias))
    else:
        monkeypatch.setenv("VIRGO_HOME", str(shared_dir))
    shared_session = make_session(trust_project_extensions=False)
    assert shared_session.tool("mcp__echo__echo") is not None
    assert "ignored project MCP configuration" not in shared_session.ui.text
    assert "untrusted" not in shared_session.ui.text.lower()
