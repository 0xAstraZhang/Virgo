"""MCP is a Host extension built on the official SDK, not a built-in plugin (S4-01..S4-08, VER-09)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from virgo.agent import ToolContext
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage

from tests.support import SUPPORT
from tests.support.harness import RecordingUI, calls, tool_results

EXTENSION = Path(__file__).resolve().parents[2] / "extensions" / "mcp.py"
SERVER = SUPPORT / "mcp_echo_server.py"


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("virgo_ext_mcp_unit", EXTENSION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_extension() -> None:
    extensions = Path(os.environ["VIRGO_HOME"]) / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXTENSION, extensions / "mcp.py")


def configure(path: Path, command: str = sys.executable, args: list[str] | None = None,
              *, env: dict | None = None, timeout: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict = {
        "command": command,
        "args": args if args is not None else [str(SERVER)],
    }
    if env is not None:
        entry["env"] = env
    if timeout is not None:
        entry["timeout"] = timeout
    path.write_text(json.dumps({"mcpServers": {"echo": entry}}), encoding="utf-8")


def test_mcp_is_not_built_in(make_session, project):
    configure(project / ".virgo" / "mcp.json")
    session = make_session(
        settings={"plugins": {"mcp": {"enabled": True}}, "features": {"mcp": True}}
    )
    assert session.tool("mcp_status") is None
    assert not any(tool.name.startswith("mcp__") for tool in session.tools_for("main"))
    assert any("mcp extension" in notice for notice in session.settings.migration_warnings)


def test_mcp_untrusted_project_config_is_ignored_with_warning(make_session, project):
    """S4-02 (R5): Project-level .virgo/mcp.json requires explicit project trust."""
    install_extension()
    configure(project / ".virgo" / "mcp.json")
    ui = RecordingUI()
    session = make_session(ui=ui, trust_project_extensions=False)
    assert session.tool("mcp__echo__echo") is None
    assert "untrusted project .virgo/mcp.json" in ui.text


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
        trust_project_extensions=True,
        settings={"execution": {"mode": "no-limit"}},
    )
    session.prompt("use mcp")
    assert "mcp__echo__echo" in calls()[0]["tools"]
    results = [message for message in session.main.messages if isinstance(message, ToolResultMessage)]
    assert [message.text() for message in results] == ["hello mcp", "3"]
    assert session.main.agent.last_text == "ok"
    context = ToolContext("direct", project, threading.Event())
    direct = session.tool("mcp__echo__add").execute({"a": 20, "b": 30}, context)
    assert direct.as_text() == "50"
    assert direct.details["boundary"]["provider"] == "host"
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
    session = make_session(trust_project_extensions=True)
    assert session.tool("mcp__echo__good") is not None
    assert session.tool("mcp__echo__bad") is None
    assert session.tool("mcp__echo__constraint") is None


def test_mcp_extension_reports_terminated_connections_and_reaps_threads(make_session, project, tmp_path):
    """S4-05: A server that exits spontaneously transitions to unavailable and reaps its loop/stderr threads."""
    install_extension()
    server = tmp_path / "exit_server.py"
    server.write_text(
        textwrap.dedent(
            """
            import json, sys, time
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
                    time.sleep(0.25)
                    sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    configure(project / ".virgo" / "mcp.json", args=[str(server)])
    session = make_session(trust_project_extensions=True)
    context = ToolContext("status", project, threading.Event())
    assert session.tool("mcp__echo__echo") is not None
    time.sleep(0.6)  # Let the server process exit and its threads drain automatically.
    assert session.tool("mcp__echo__echo").execute({}, context).is_error
    assert '"state": "unavailable"' in session.tool("mcp_status").execute({}, context).as_text()
    # Event loop and stderr threads should have exited automatically without calling shutdown yet.
    alive_mcp_threads = [t.name for t in threading.enumerate() if t.name.startswith("mcp-echo")]
    assert not alive_mcp_threads


def test_mcp_extension_ignores_malformed_config(make_session, project):
    install_extension()
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text("[]", encoding="utf-8")
    session = make_session(trust_project_extensions=True)
    assert "mcp: invalid" in session.ui.text
    assert session.tool("mcp_status") is not None


def test_ver09_jsonrpc_error_keeps_connection_healthy_and_shared_across_subagents(make_session, project, tmp_path):
    """VER-09 (S4-01, S4-03): -32602 keeps connection healthy; two subagents reuse one shared Host server PID."""
    install_extension()
    server = tmp_path / "rpc_pid_server.py"
    server.write_text(
        textwrap.dedent(
            """
            import json, os, sys
            for line in sys.stdin:
                request = json.loads(line)
                if "id" not in request:
                    continue
                rid, method = request["id"], request.get("method")
                if method == "initialize":
                    result = {"protocolVersion": request["params"]["protocolVersion"],
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "pid", "version": "1"}}
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
                elif method == "tools/list":
                    result = {"tools": [{
                        "name": "ping", "description": "Return server PID or fail with -32602",
                        "annotations": {"readOnlyHint": True},
                        "inputSchema": {"type": "object", "properties": {"fail": {"type": "boolean"}}},
                    }]}
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
                elif method == "tools/call":
                    args = (request.get("params") or {}).get("arguments") or {}
                    if args.get("fail"):
                        print(json.dumps({
                            "jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32602, "message": "Invalid params: bad argument"},
                        }), flush=True)
                    else:
                        print(json.dumps({
                            "jsonrpc": "2.0", "id": rid,
                            "result": {"content": [{"type": "text", "text": str(os.getpid())}]},
                        }), flush=True)
            """
        ),
        encoding="utf-8",
    )
    configure(Path(os.environ["VIRGO_HOME"]) / "mcp.json", args=[str(server)])
    session = make_session(settings={"execution": {"mode": "no-limit"}})
    ctx = ToolContext("call", project, threading.Event())
    ping = session.tool("mcp__echo__ping")
    assert ping is not None

    # 1. Trigger a -32602 JSON-RPC error -> must NOT poison the connection.
    bad = ping.execute({"fail": True}, ctx)
    assert bad.is_error
    assert "Invalid params" in bad.as_text()
    status_after_err = session.tool("mcp_status").execute({"server": "echo"}, ctx).as_text()
    assert '"state": "ready"' in status_after_err

    # 2. Next call still succeeds on the same live server process.
    first_pid = ping.execute({"fail": False}, ctx).as_text()
    assert first_pid.isdigit()

    # 3. Two distinct subagents reuse the EXACT SAME shared Host MCP server process (S4-01).
    child1 = session.create_runtime("sub1", "subagent", parent=session.main)
    child2 = session.create_runtime("sub2", "subagent", parent=session.main)
    try:
        sub1_ping = next(t for t in child1.tools if t.name == "mcp__echo__ping")
        sub2_ping = next(t for t in child2.tools if t.name == "mcp__echo__ping")
        pid1 = sub1_ping.execute({}, ToolContext("c1", project, threading.Event(), agent="sub1", origin=child1.origin)).as_text()
        pid2 = sub2_ping.execute({}, ToolContext("c2", project, threading.Event(), agent="sub2", origin=child2.origin)).as_text()
        assert pid1 == pid2 == first_pid
    finally:
        session.release_runtime(child1)
        session.release_runtime(child2)


def test_ver09_connect_cancellation_interrupts_hanging_server_without_deadlock(tmp_path):
    """VER-09 (S4-04): Esc/cancellation interrupts a hanging MCP handshake promptly."""
    mcp_mod = load_mcp_module()
    from virgo.execution import HostContext

    hang = tmp_path / "hang_server.py"
    hang.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    cancel = threading.Event()
    conn = mcp_mod._Connection(
        "hang",
        {"command": sys.executable, "args": [str(hang)], "timeout": 30},
        HostContext.unrestricted(tmp_path),
        signal=cancel,
    )
    timer = threading.Timer(0.25, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(mcp_mod.MCPCallCancelled):
            conn.connect()
    finally:
        timer.cancel()
        conn.close()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"Cancellation took too long: {elapsed:.2f}s"
    assert conn.closed


def test_s405_s406_crashed_server_rejected_and_stderr_redacted_with_boundary(tmp_path):
    """S4-05 & S4-06: Crashed server is rejected by connect() with redacted stderr and boundary metadata."""
    mcp_mod = load_mcp_module()
    from virgo.execution import HostContext

    secret = "ghp_super_secret_token_98765"
    crasher = tmp_path / "crash_server.py"
    crasher.write_text(
        "import os, sys\n"
        "sys.stderr.write(f'startup boom token={os.environ.get(\"API_TOKEN\")}\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    conn = mcp_mod._Connection(
        "crash",
        {"command": sys.executable, "args": [str(crasher)], "env": {"API_TOKEN": secret}, "timeout": 5},
        HostContext.unrestricted(tmp_path),
    )
    with pytest.raises(mcp_mod.MCPConnectionError) as exc_info:
        conn.connect()
    err = exc_info.value
    assert "startup boom" in str(err)
    assert secret not in str(err)
    assert secret not in err.stderr
    assert err.boundary is not None and err.boundary["provider"] == "host"


def test_s402_always_allow_bound_to_server_config_fingerprint(broker_session, tmp_path):
    """S4-02: 'Always allow' persists for identical server config fingerprint and invalidates when config changes."""
    install_extension()
    cfg_path = Path(os.environ["VIRGO_HOME"]) / "mcp.json"
    configure(cfg_path, args=[str(SERVER)], env={"SERVER_TAG": "v1"})
    ui = RecordingUI(["a", "n"])
    session = broker_session(
        faux.reply(tools=[("mcp__echo__add", {"a": 1, "b": 2})]),
        faux.reply(tools=[("mcp__echo__add", {"a": 3, "b": 4})]),
        faux.reply("done"),
        ui=ui,
        preset="workspace-edit",
        extensions=True,
    )
    session.prompt("Add twice")
    results = tool_results(session)
    assert len(results) == 2
    assert results[0].text() == "3" and not results[0].is_error
    assert results[1].text() == "7" and not results[1].is_error
    # First call prompted ("a"), second call reused saved fingerprint-bound grant without prompting.
    assert len(ui.questions) == 1

    # Change server env -> config fingerprint changes -> reconnected tool must prompt again (and "n" blocks it).
    configure(cfg_path, args=[str(SERVER)], env={"SERVER_TAG": "v2"})
    session.prompt("/mcp reconnect")
    faux.push("test", faux.reply(tools=[("mcp__echo__add", {"a": 5, "b": 6})]), faux.reply("done"))
    session.prompt("Add after config change")
    latest = tool_results(session)[-1]
    assert latest.is_error
    assert len(ui.questions) == 2


def test_s407_config_validation_rejects_empty_and_non_string_fields(monkeypatch):
    """S4-07: Strict validation rejects empty expanded commands and non-string args/env values."""
    mcp_mod = load_mcp_module()
    monkeypatch.delenv("VIRGO_UNSET_MCP_CMD", raising=False)
    for bad in (
        {"command": ""},
        {"command": "   "},
        {"command": "${VIRGO_UNSET_MCP_CMD}"},
        {"command": "python", "args": [None]},
        {"command": "python", "args": [123]},
        {"command": "python", "env": {"KEY": None}},
        {"command": "python", "env": {"KEY": 42}},
        {"command": "python", "timeout": True},
        {"command": "python", "timeout": -1},
    ):
        with pytest.raises(mcp_mod.MCPConnectionError):
            mcp_mod._validate_config(bad)


def test_ver09_s408_windows_host_path_and_cmd_bat_resolution():
    """VER-09 (S4-08): Native Windows Host MCP resolves commands via PATH and wraps .cmd/.bat with cmd.exe."""
    mcp_mod = load_mcp_module()

    def fake_which(cmd, path=None):
        return {
            "npx": r"C:\Program Files\nodejs\npx.cmd",
            "tool.bat": r"C:\tools\tool.bat",
            "python": r"C:\Python313\python.exe",
        }.get(cmd)

    npx_argv = mcp_mod._resolve_host_argv(
        "npx", ["-y", "@modelcontextprotocol/server-filesystem", "C:\\my repo"],
        {"PATH": r"C:\Program Files\nodejs", "COMSPEC": r"C:\Windows\System32\cmd.exe"},
        platform_nt=True, which=fake_which,
    )
    assert npx_argv == (
        r"C:\Windows\System32\cmd.exe", "/d", "/c", "call",
        r"C:\Program Files\nodejs\npx.cmd", "-y", "@modelcontextprotocol/server-filesystem", "C:\\my repo",
    )

    bat_argv = mcp_mod._resolve_host_argv(
        "tool.bat", ["--flag"], {"COMSPEC": "cmd.exe"}, platform_nt=True, which=fake_which,
    )
    assert bat_argv == ("cmd.exe", "/d", "/c", "call", r"C:\tools\tool.bat", "--flag")

    exe_argv = mcp_mod._resolve_host_argv(
        "python", ["-V"], {}, platform_nt=True, which=fake_which,
    )
    assert exe_argv == (r"C:\Python313\python.exe", "-V")


def test_mcp_tool_crash_stderr_is_retained_in_diagnostic_get(make_session, project, tmp_path):
    """Real MCP tool failure stderr is forwarded through Runtime into DiagnosticAccess section='stderr'."""
    install_extension()
    server = tmp_path / "crash_on_call_server.py"
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
                              "serverInfo": {"name": "crash", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "boom", "description": "crash on call",
                                         "inputSchema": {"type": "object", "properties": {}},
                                         "annotations": {"readOnlyHint": True}}]}
                else:
                    sys.stderr.write("mcp-real-stderr-sentinel: fatal crash\\n")
                    sys.stderr.flush()
                    sys.exit(2)
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    configure(Path(os.environ["VIRGO_HOME"]) / "mcp.json", args=[str(server)])
    session = make_session(
        faux.reply(tools=[("mcp__echo__boom", {})]),
        faux.reply("done"),
        extensions=True,
    )
    session.prompt("Trigger failing MCP tool")
    failed = tool_results(session)[-1]
    assert failed.is_error
    error_id = failed.details["error"]["error_id"]
    stderr_page = session.main.diagnostic_access.read(error_id, "stderr")
    assert stderr_page["status"] == "ok"
    assert "mcp-real-stderr-sentinel" in stderr_page["text"]
    summary_page = json.loads(session.main.diagnostic_access.read(error_id, "summary")["text"])
    assert summary_page["context"]["server"] == "echo"
    assert "stderr" not in summary_page["context"]
