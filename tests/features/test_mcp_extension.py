"""MCP is a built-in Host extension on the official SDK with lazy SDK loading (S4-01..S4-08, VER-09)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from virgo.agent import ToolContext
from virgo.ai.providers import faux
from virgo.ai.types import ImageContent, TextContent, ToolResultMessage
from virgo.extensions import mcp as mcp_ext
from virgo.i18n import Translator
from virgo.output_store import OutputPipe
from virgo.tools.output import MAX_BYTES, MAX_LINES

from tests.support import SUPPORT
from tests.support.harness import RecordingUI, calls, tool_results, wait_until

SERVER = SUPPORT / "mcp_echo_server.py"
REPORT = "mcp__echo__report"


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


@pytest.fixture
def report_session(make_session, project):
    """Serve a supplied CallToolResult over stdio and count remote dispatches."""
    server = project / "report.py"
    server.write_text(textwrap.dedent("""
        import json, sys
        from pathlib import Path

        for line in sys.stdin:
            req = json.loads(line)
            if "id" not in req:
                continue
            if req["method"] == "initialize":
                result = {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "report", "version": "1"}}
            elif req["method"] == "tools/list":
                result = {"tools": [{"name": "report", "description": "Read the report",
                                     "inputSchema": {"type": "object", "properties": {}}}]}
            else:
                with Path("dispatches").open("a") as log:
                    print(req["params"]["name"], file=log)
                result = json.loads(Path("response.json").read_text(encoding="utf-8"))
            print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
    """), encoding="utf-8")
    configure(project / ".virgo" / "mcp.json", args=[str(server)])
    session = make_session()
    wait_until(lambda: session.tool(REPORT) is not None)
    return session


def set_report(session, *content, **fields):
    (session.cwd / "response.json").write_text(json.dumps({"content": content, **fields}), encoding="utf-8")


def context(runtime, **kwargs):
    return ToolContext("report", runtime.cwd, runtime.agent.signal, runtime.name, origin=runtime.origin, **kwargs)


def output_id(preview):
    assert len(preview.encode()) <= MAX_BYTES and len(preview.splitlines()) <= MAX_LINES
    assert "read_output" in preview
    [identifier] = re.findall(r"\bout_[0-9a-f]+\b", preview)
    return identifier


def test_mcp_bounds_mixed_error_result_before_session_hook(report_session):
    session = report_session
    large = "x" * (MAX_BYTES + 1)
    small = "  keep this\n\n"
    set_report(session, {"type": "text", "text": small}, {"type": "image", "mimeType": "image/png", "data": "AQ=="},
               {"type": "text", "text": large}, {"type": "image", "mimeType": "image/jpeg", "data": "Ag=="},
               isError=True)
    observed = []
    session.bus.on("tool_result", lambda res, *_: observed.append(list(res.content)))
    faux.script("test", faux.reply(tools=[(REPORT, {})]), faux.reply("done"))
    session.prompt("get the report")
    [result] = tool_results(session)
    [blocks] = observed
    assert result.is_error and result.details["mcp"] and result.details["server"] == "echo"
    assert result.details["tool"] == REPORT and result.details["boundary"]["provider"] == "host"
    assert blocks == result.content
    first, png, preview, jpeg = blocks
    assert (first, png, jpeg) == (TextContent(small), ImageContent("AQ==", "image/png"), ImageContent("Ag==", "image/jpeg"))
    identifier = output_id(preview.text)
    store = session.main.execution_context.outputs
    assert [path.stem for path in store.directory.glob("*.log")] == [identifier]
    read = session.tool("read_output")
    page = read.execute({"output_id": identifier}, context(session.main))
    assert page.as_text() == large[:MAX_BYTES] and page.details["next_offset"] == MAX_BYTES
    last = read.execute({"output_id": identifier, "offset": MAX_BYTES}, context(session.main))
    assert last.as_text() == large[MAX_BYTES:] and last.details["next_offset"] is None


def test_mcp_each_large_block_uses_originating_subagent_store(report_session):
    session = report_session
    child = session.create_runtime("worker", "subagent", parent=session.main)
    try:
        session.new_session()
        lines = "ok\n" * (MAX_LINES + 1)
        resource = "資" * (MAX_BYTES // 3 + 1)
        set_report(session, {"type": "text", "text": lines},
                   {"type": "resource", "resource": {"uri": "file:///report", "text": resource}})
        caller = context(child, translator=Translator("zh-CN"))
        result = session.tool(REPORT).execute({}, caller)
        assert not result.is_error
        store = child.execution_context.outputs
        identifiers = []
        for block, original in zip(result.content, (lines, resource), strict=True):
            assert "输出已截断" in block.text
            identifier = output_id(block.text)
            identifiers.append(identifier)
            assert store.read(store.lookup(identifier), limit=len(original.encode()))[0] == original.encode()
            foreign = session.tool("read_output").execute({"output_id": identifier}, context(session.main))
            assert foreign.is_error
        assert len(set(identifiers)) == 2
        assert {path.stem for path in store.directory.glob("*.log")} == set(identifiers)
        assert not list(session.main.execution_context.outputs.directory.glob("*.log"))
    finally:
        session.release_runtime(child)


def test_mcp_bounds_structured_only_result(report_session):
    session = report_session
    data = {"title": "測試 " * MAX_LINES * 4}
    set_report(session, structuredContent=data)
    result = session.tool(REPORT).execute({}, context(session.main))
    assert not result.is_error
    store = session.main.execution_context.outputs
    rec = store.lookup(output_id(result.as_text()))
    rendered = json.dumps(data, ensure_ascii=False).encode()
    assert store.read(rec, limit=len(rendered))[0] == rendered


def test_mcp_bounds_unknown_sdk_block_rendered_as_json(report_session, monkeypatch):
    # The current wire schema rejects unknown types. Exercise the adapter's fallback
    # with the interface a future SDK content type would expose.
    session = report_session
    data = {"type": "future", "body": "界" * MAX_BYTES}
    block = SimpleNamespace(type="future", model_dump=lambda **kwargs: data)
    reply = SimpleNamespace(content=[block], is_error=False)
    monkeypatch.setattr(mcp_ext._Connection, "call", lambda *args: reply)
    result = session.tool(REPORT).execute({}, context(session.main))
    assert not result.is_error
    store = session.main.execution_context.outputs
    rec = store.lookup(output_id(result.as_text()))
    rendered = json.dumps(data, ensure_ascii=False).encode()
    assert store.read(rec, limit=len(rendered))[0] == rendered


def test_mcp_exact_limits_and_empty_results_do_not_spill(report_session):
    session = report_session
    exact = " \n" * (MAX_LINES - 1)
    exact += " " * (MAX_BYTES - len(exact))
    direct = ToolContext("direct", session.cwd, threading.Event())
    set_report(session, {"type": "text", "text": exact}, {"type": "text", "text": ""},
               {"type": "resource", "resource": {"uri": "file:///empty", "text": ""}})
    for caller in (direct, context(session.main)):
        result = session.tool(REPORT).execute({}, caller)
        assert not result.is_error and result.content == [TextContent(exact), TextContent(""), TextContent("")]
    set_report(session)
    assert session.tool(REPORT).execute({}, direct).content == [TextContent("(no content)")]
    assert not list((Path(os.environ["VIRGO_HOME"]) / "outputs").rglob("*.log"))


@pytest.mark.parametrize("failure", ["no origin", "invalid origin", "storage error", "partial write"])
def test_mcp_spill_failure_reports_completed_dispatch(report_session, monkeypatch, failure):
    session = report_session
    caller = context(session.main)
    if failure == "storage error":
        def fail_create(prefix):
            raise OSError("disk full")
        monkeypatch.setattr(session.main.execution_context.outputs, "create", fail_create)
    elif failure == "partial write":
        write = OutputPipe.write

        def fail_write(stream, data):
            write(stream, data[:100])
            raise OSError("disk full")

        monkeypatch.setattr(OutputPipe, "write", fail_write)
    elif failure == "invalid origin":
        caller.agent = "wrong-runtime"
    else:
        caller.origin = None
    set_report(session, {"type": "text", "text": "first"}, {"type": "text", "text": "private response " * 4000},
               {"type": "image", "mimeType": "image/png", "data": "AQ=="})
    result = session.tool(REPORT).execute({}, caller)
    assert result.is_error
    first, error, image = result.content
    assert first == TextContent("first") and image == ImageContent("AQ==", "image/png")
    message = error.text.lower()
    assert "already ran" in message and "private response" not in message and len(message.encode()) <= MAX_BYTES
    assert ("originating runtime" if "origin" in failure else "stor") in message
    assert result.details["tool"] == REPORT and result.details["boundary"]["payload_started"]
    assert (session.cwd / "dispatches").read_text().splitlines() == ["report"]
    assert not list((Path(os.environ["VIRGO_HOME"]) / "outputs").rglob("*.log"))


def test_mcp_builtin_lazy_sdk_and_dynamic_status(make_session, project, tmp_path):
    """Built-in mcp keeps SDK unimported and hides mcp_status when no servers are configured."""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from virgo.harness.session import AgentSession\n"
                f"s = AgentSession({str(project)!r}, model='faux/test', session=None, "
                "refresh_models=False, settings={'execution': {'mode': 'no-limit'}})\n"
                "try:\n"
                "    assert 'builtin:mcp' in s.loaded_plugins\n"
                "    assert s.commands.get('mcp') is not None\n"
                "    assert s.tool('mcp_status') is None\n"
                "    assert 'mcp' not in sys.modules\n"
                "finally:\n"
                "    s.shutdown()\n"
            ),
        ],
        env={**os.environ, "VIRGO_HOME": str(tmp_path / "clean-home")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, f"stdout={probe.stdout}\nstderr={probe.stderr}"

    # No configured servers -> /mcp exists, mcp_status is hidden.
    session = make_session(settings={"execution": {"mode": "no-limit"}})
    assert session.commands.get("mcp") is not None
    assert session.tool("mcp_status") is None
    assert not any(tool.name.startswith("mcp__") for tool in session.tools_for("main"))
    session.prompt("/mcp")
    assert "No MCP servers configured" in session.ui.text

    # Reconnecting onto a broken server dynamically publishes mcp_status so the failure is inspectable.
    cfg = Path(os.environ["VIRGO_HOME"]) / "mcp.json"
    configure(cfg, command="/nonexistent/mcp-server", args=[], timeout=5)
    session.prompt("/mcp reconnect")
    assert session.tool("mcp_status") is not None
    status = session.tool("mcp_status").execute(
        {}, ToolContext("status", project, threading.Event())
    ).as_text()
    assert '"server": "echo"' in status and '"state": "unavailable"' in status

    # Reconnecting back to an empty config hides mcp_status again.
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    session.prompt("/mcp reconnect")
    assert session.tool("mcp_status") is None


def test_mcp_builtin_disable_override_and_untrusted_project_isolation(make_session, project):
    """Built-in mcp respects --no-extensions, extensions.disabled, user overrides, and R5 project trust."""
    configure(Path(os.environ["VIRGO_HOME"]) / "mcp.json")

    no_ext = make_session(extensions=False, settings={"execution": {"mode": "no-limit"}})
    assert no_ext.commands.get("mcp") is None
    assert no_ext.tool("mcp_status") is None
    assert no_ext.tool("mcp__echo__echo") is None

    disabled = make_session(
        settings={"execution": {"mode": "no-limit"}, "extensions": {"disabled": ["mcp"]}}
    )
    assert disabled.commands.get("mcp") is None
    assert disabled.tool("mcp_status") is None
    assert disabled.tool("mcp__echo__echo") is None

    # Retired plugin/feature toggles fail clearly instead of silently migrating.
    with pytest.raises(ValueError, match="Unknown plugin 'mcp'"):
        make_session(settings={"plugins": {"mcp": {"enabled": True}}})
    with pytest.raises(ValueError, match="Unknown legacy feature 'mcp'"):
        make_session(settings={"features": {"mcp": True}})

    # Untrusted project cannot disable built-in mcp via .virgo/settings.json.
    (project / ".virgo").mkdir(parents=True, exist_ok=True)
    (project / ".virgo" / "settings.json").write_text(
        json.dumps({"extensions": {"disabled": ["mcp"]}}),
        encoding="utf-8",
    )
    untrusted = make_session(trust_project_extensions=False, settings={"execution": {"mode": "no-limit"}})
    assert untrusted.commands.get("mcp") is not None
    try:
        wait_until(lambda: untrusted.tool("mcp__echo__echo") is not None)
    except AssertionError:
        untrusted.prompt("/mcp")
        pytest.fail(f"The echo tool was not published. MCP status:\n{untrusted.ui.text}")

    # User override ~/.virgo/extensions/mcp.py replaces built-in mcp cleanly without collision.
    (project / ".virgo" / "settings.json").unlink()
    user_ext = Path(os.environ["VIRGO_HOME"]) / "extensions"
    user_ext.mkdir(parents=True, exist_ok=True)
    (user_ext / "mcp.py").write_text(
        "def setup(api):\n"
        "    api.register_command('mcp', lambda s, a: api.ui.print('custom mcp override'), 'custom')\n",
        encoding="utf-8",
    )
    overridden = make_session(settings={"execution": {"mode": "no-limit"}})
    assert "user:mcp" in overridden.loaded_plugins
    assert "builtin:mcp" not in overridden.loaded_plugins
    assert overridden.tool("mcp__echo__echo") is None
    overridden.prompt("/mcp")
    assert "custom mcp override" in overridden.ui.text


def test_mcp_untrusted_project_config_is_ignored_with_warning(make_session, project):
    """S4-02 (R5): Project-level .virgo/mcp.json requires explicit project trust."""
    configure(project / ".virgo" / "mcp.json")
    ui = RecordingUI()
    session = make_session(ui=ui, trust_project_extensions=False)
    assert session.tool("mcp__echo__echo") is None
    assert session.tool("mcp_status") is None
    assert "untrusted project .virgo/mcp.json" in ui.text


def test_mcp_extension_exposes_and_rebinds_tools(make_session, project):
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

    # Removing the server removes its tools and hides mcp_status at the next publication.
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    session.prompt("/mcp reconnect")
    assert session.tool("mcp__echo__add") is None
    assert session.tool("mcp_status") is None
    assert not any(tool.name.startswith("mcp__") for tool in session.tools_for("main"))

    configure(config)
    session.prompt("/mcp reconnect")
    assert session.tool("mcp__echo__echo") is not None
    assert session.tool("mcp_status") is not None


def test_mcp_extension_reports_unavailable_servers_without_stopping(make_session):
    session = make_session(
        settings={"mcpServers": {"broken": {"command": "/nonexistent/mcp-server", "timeout": 5}}}
    )
    status = session.tool("mcp_status").execute(
        {}, ToolContext("status", session.cwd, threading.Event())
    ).as_text()
    assert '"server": "broken"' in status and '"state": "unavailable"' in status
    assert session.tool("mcp_status") is not None


def test_mcp_extension_skips_unsupported_schemas(make_session, project, tmp_path):
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
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text("[]", encoding="utf-8")
    session = make_session(trust_project_extensions=True)
    assert "mcp: invalid" in session.ui.text
    assert session.commands.get("mcp") is not None
    assert session.tool("mcp_status") is None


def test_ver09_jsonrpc_error_keeps_connection_healthy_and_shared_across_subagents(make_session, project, tmp_path):
    """VER-09 (S4-01, S4-03): -32602 keeps connection healthy; two subagents reuse one shared Host server PID."""
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
    mcp_mod = mcp_ext
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
    mcp_mod = mcp_ext
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
    mcp_mod = mcp_ext
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
    mcp_mod = mcp_ext

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


def test_mcp_bounded_startup_does_not_block_cold_start_and_cancels_cleanly(make_session, project, tmp_path):
    """A hanging MCP server must not block AgentSession startup past the bounded grace window."""
    hang = tmp_path / "hang_mcp.py"
    hang.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    cfg = Path(os.environ["VIRGO_HOME"]) / "mcp.json"
    cfg.write_text(
        json.dumps({
            "mcpServers": {
                "fast": {"command": sys.executable, "args": [str(SERVER)]},
                "slow": {"command": sys.executable, "args": [str(hang)], "timeout": 30},
            }
        }),
        encoding="utf-8",
    )
    started = time.monotonic()
    session = make_session(settings={"execution": {"mode": "no-limit"}})
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"MCP startup blocked cold start for {elapsed:.2f}s"
    # Fast server is already published; slow server reports connecting.
    assert session.tool("mcp__fast__echo") is not None
    status = json.loads(
        session.tool("mcp_status").execute({}, ToolContext("st", project, threading.Event())).as_text()
    )
    by_name = {row["server"]: row for row in status["servers"]}
    assert by_name["fast"]["state"] == "ready"
    assert by_name["slow"]["state"] == "connecting"
    assert session.tool("mcp_status").execution_scope == "broker"
    # Shutting down cancels the hanging handshake promptly without leaking threads.
    close_start = time.monotonic()
    session.shutdown()
    assert time.monotonic() - close_start < 5.0
    alive = [t.name for t in threading.enumerate() if t.name.startswith("mcp-slow")]
    assert not alive


def test_mcp_tool_name_collision_disambiguation_and_filtering(make_session, project, tmp_path):
    """Long truncated names and punctuation-normalized duplicates are disambiguated; include/exclude filter tools."""
    prefix = "create_pull_request_review_comment_for_enterprise_repository_"
    server = tmp_path / "collision_server.py"
    server.write_text(
        textwrap.dedent(
            f"""
            import json, sys
            for line in sys.stdin:
                req = json.loads(line)
                if "id" not in req:
                    continue
                rid, method = req["id"], req.get("method")
                if method == "initialize":
                    res = {{"protocolVersion": req["params"]["protocolVersion"],
                           "capabilities": {{"tools": {{}}}},
                           "serverInfo": {{"name": "col", "version": "1"}}}}
                elif method == "tools/list":
                    res = {{"tools": [
                        {{"name": "{prefix}alpha", "description": "alpha", "inputSchema": {{"type": "object", "properties": {{}}}}}},
                        {{"name": "{prefix}beta", "description": "beta", "inputSchema": {{"type": "object", "properties": {{}}}}}},
                        {{"name": "foo.bar", "description": "dot", "inputSchema": {{"type": "object", "properties": {{}}}}}},
                        {{"name": "foo_bar", "description": "under", "inputSchema": {{"type": "object", "properties": {{}}}}}},
                        {{"name": "secret_admin", "description": "excluded", "inputSchema": {{"type": "object", "properties": {{}}}}}},
                    ]}}
                else:
                    res = {{"content": [{{"type": "text", "text": req["params"]["name"]}}]}}
                print(json.dumps({{"jsonrpc": "2.0", "id": rid, "result": res}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    cfg = Path(os.environ["VIRGO_HOME"]) / "mcp.json"
    cfg.write_text(
        json.dumps({
            "mcpServers": {
                "col": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "excludeTools": ["secret_*"],
                }
            }
        }),
        encoding="utf-8",
    )
    session = make_session(settings={"execution": {"mode": "no-limit"}})
    mcp_tools = [t for t in session.tools_for("main") if t.name.startswith("mcp__col__")]
    assert len(mcp_tools) == 4
    assert all(len(t.name) <= 64 for t in mcp_tools)
    assert not any("secret_admin" in t.name for t in mcp_tools)
    ctx = ToolContext("c", project, threading.Event())
    returned = {t.execute({}, ctx).as_text() for t in mcp_tools}
    assert returned == {f"{prefix}alpha", f"{prefix}beta", "foo.bar", "foo_bar"}


def test_mcp_config_precedence_and_reconnect_reloads_settings_and_call_timeout(make_session, project, tmp_path):
    """Trusted project mcp.json overrides user settings.json; reconnect reloads settings.json; callTimeout & cancel work."""
    home = Path(os.environ["VIRGO_HOME"])
    slow = tmp_path / "slow_call.py"
    slow.write_text(
        textwrap.dedent(
            """
            import json, sys, time
            for line in sys.stdin:
                req = json.loads(line)
                if "id" not in req:
                    continue
                rid, method = req["id"], req.get("method")
                if method == "initialize":
                    res = {"protocolVersion": req["params"]["protocolVersion"],
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "slow", "version": "1"}}
                elif method == "tools/list":
                    res = {"tools": [
                        {"name": "sleep", "description": "sleep", "inputSchema": {"type": "object", "properties": {}}},
                    ]}
                else:
                    time.sleep(5)
                    res = {"content": [{"type": "text", "text": "late"}]}
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    # User settings.json defines 'svc' -> broken; trusted project .virgo/mcp.json overrides 'svc' -> echo server.
    (home / "settings.json").write_text(
        json.dumps({"mcpServers": {"svc": {"command": "/nonexistent/user-cmd"}}}),
        encoding="utf-8",
    )
    (project / ".virgo").mkdir(parents=True, exist_ok=True)
    (project / ".virgo" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"svc": {"command": sys.executable, "args": [str(SERVER)], "includeTools": ["echo"]}}}),
        encoding="utf-8",
    )
    session = make_session(trust_project_extensions=True, settings={"execution": {"mode": "no-limit"}})
    assert session.tool("mcp__svc__echo") is not None
    assert session.tool("mcp__svc__add") is None  # filtered out by includeTools

    # Removing project override and updating user settings.json is picked up by /mcp reconnect.
    (project / ".virgo" / "mcp.json").unlink()
    (home / "settings.json").write_text(
        json.dumps({"mcpServers": {"svc": {"command": sys.executable, "args": [str(slow)], "callTimeout": 0.25}}}),
        encoding="utf-8",
    )
    session.prompt("/mcp reconnect")
    sleep_tool = session.tool("mcp__svc__sleep")
    assert sleep_tool is not None

    # Cancelled call marks details['cancelled'] = True.
    aborted = threading.Event()
    aborted.set()
    cancel_res = sleep_tool.execute({}, ToolContext("cancel", project, aborted))
    assert cancel_res.is_error and cancel_res.details.get("cancelled") is True

    # Timed-out call uses per-server callTimeout and guides /mcp reconnect.
    timeout_res = sleep_tool.execute({}, ToolContext("to", project, threading.Event()))
    assert timeout_res.is_error and "/mcp reconnect" in timeout_res.as_text()
    # Subsequent call on dead connection also guides /mcp reconnect.
    dead_res = sleep_tool.execute({}, ToolContext("dead", project, threading.Event()))
    assert dead_res.is_error and "/mcp reconnect" in dead_res.as_text()


def test_mcp_colliding_server_names_are_order_independent_across_startup_and_reconnect(
    make_session, project, tmp_path
):
    """Servers normalizing identically (e.g. prod.api and prod_api) keep stable tool names regardless of arrival order."""
    server = tmp_path / "tagged_server.py"
    server.write_text(
        textwrap.dedent(
            """
            import json, os, sys, time
            delay = float(os.environ.get("HANDSHAKE_DELAY", "0"))
            tag = os.environ.get("SERVER_TAG", "unknown")
            for line in sys.stdin:
                req = json.loads(line)
                if "id" not in req:
                    continue
                rid, method = req["id"], req.get("method")
                if method == "initialize":
                    if delay > 0:
                        time.sleep(delay)
                    res = {"protocolVersion": req["params"]["protocolVersion"],
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": tag, "version": "1"}}
                elif method == "tools/list":
                    res = {"tools": [
                        {"name": "update", "description": f"update {tag}",
                         "inputSchema": {"type": "object", "properties": {}}},
                    ]}
                else:
                    res = {"content": [{"type": "text", "text": tag}]}
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    cfg = Path(os.environ["VIRGO_HOME"]) / "mcp.json"
    # prod.api is configured first but finishes handshake AFTER prod_api.
    cfg.write_text(
        json.dumps({
            "mcpServers": {
                "prod.api": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {"SERVER_TAG": "dot", "HANDSHAKE_DELAY": "0.3"},
                },
                "prod_api": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {"SERVER_TAG": "under", "HANDSHAKE_DELAY": "0"},
                },
            }
        }),
        encoding="utf-8",
    )
    session = make_session(settings={"execution": {"mode": "no-limit"}})
    ctx = ToolContext("c", project, threading.Event())
    # Neither colliding server claims the unsuffixed ambiguous name.
    assert session.tool("mcp__prod_api__update") is None
    before_tools = [t for t in session.tools_for("main") if t.name.startswith("mcp__prod_api__update_")]
    assert len(before_tools) == 2
    before_map = {t.name: t.execute({}, ctx).as_text() for t in before_tools}
    assert set(before_map.values()) == {"dot", "under"}

    # Reconnecting (config order, reversed delays) must preserve the exact name -> server mapping.
    cfg.write_text(
        json.dumps({
            "mcpServers": {
                "prod.api": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {"SERVER_TAG": "dot", "HANDSHAKE_DELAY": "0"},
                },
                "prod_api": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {"SERVER_TAG": "under", "HANDSHAKE_DELAY": "0.3"},
                },
            }
        }),
        encoding="utf-8",
    )
    session.prompt("/mcp reconnect")
    after_tools = [t for t in session.tools_for("main") if t.name.startswith("mcp__prod_api__update_")]
    after_map = {t.name: t.execute({}, ctx).as_text() for t in after_tools}
    assert after_map == before_map
