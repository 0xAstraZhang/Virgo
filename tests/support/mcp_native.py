"""The same real local MCP boundary acceptance for every native backend."""

import json
import shutil
import sys
import threading
from importlib.metadata import version
from pathlib import Path

from virgo.agent import ToolContext
from virgo.ai.providers import faux
from virgo.harness.extensions import ExtensionAPI, _load_file

from tests.support.harness import tool_results


def _official_sdk(native, project, extension, monkeypatch):
    """Record real runtime support separately from synchronous MCP isolation."""
    sdk_server = project / "mcp_sdk_server.py"
    shutil.copy(Path(__file__).with_name("mcp_sdk_server.py"), sdk_server)
    launches = []
    executor = native.main.execution_context.executor
    run_process = executor.run_process

    def observe(request, policy, cancellation=None):
        if request.argv[-1] == str(sdk_server):
            launches.append({"provider": policy.provider, "network_allowed": policy.network_allowed})
        return run_process(request, policy, cancellation)

    with monkeypatch.context() as instrumentation:
        instrumentation.setattr(executor, "run_process", observe)
        observed = {}
        for index, mode in enumerate(("read-only", "workspace-edit")):
            native.services["execution"].select_mode(mode)
            policy = native.main.execution_context.policy
            network_allowed = mode == "workspace-edit"
            assert policy.network_allowed is network_allowed and not policy.unrestricted
            # Distinct names prevent a failed reload's retained old connection
            # from being mistaken for the newly requested boundary.
            name = "sdk_" + mode.replace("-", "_")
            (native.paths.user / "mcp.json").write_text(json.dumps({"mcpServers": {name: {
                "command": sys.executable, "args": ["-I", str(sdk_server)],
            }}}), encoding="utf-8")
            if index == 0:
                _load_file(native, extension)
                native.bus.emit("session_start", native)
            else:
                native.prompt("/mcp reconnect")
            sdk_tool = native.tool(f"mcp__{name}__probe")
            status = native.tool("mcp_status").execute(
                {"server": name}, ToolContext("sdk-status", project, threading.Event()))
            assert len(launches) == index + 1, launches
            assert launches[-1] == {"provider": "sandbox", "network_allowed": network_allowed}, launches
            assert native.main.execution_context.policy == policy
            if sdk_tool is None:
                # CPython first creates a TCP socket while importing _overlapped;
                # a later stage creates the event loop's loopback wakeup pair.
                # Accept only the precise OS denial at these verified stages.
                assert sys.platform == "win32", status.as_text()
                row, = json.loads(status.as_text())["servers"]
                error = row["error"] or ""
                assert row["state"] == "unavailable" and row["tools"] == 0, row
                assert "PermissionError: [WinError 10013]" in error, error
                stages = {
                    "overlapped_import": ("import asyncio", "windows_events.py", "import _overlapped"),
                    "socketpair_wakeup": ("_make_self_pipe", "socket.py", "socketpair"),
                }
                stage = next((stage for stage, parts in stages.items() if all(part in error for part in parts)), None)
                assert stage is not None, error
                assert not (project / "sdk-written.txt").exists()
                observed[mode] = {"status": "network_initialization_denied", "stage": stage,
                                  "winerror": 10013, "diagnostic": error}
                continue
            context = ToolContext("sdk", project, threading.Event(), origin=native.main.origin)
            result = sdk_tool.execute({"path": "sdk-written.txt"} if mode == "workspace-edit" else {}, context)
            assert not result.is_error, result.as_text()
            facts = json.loads(result.as_text())
            assert facts["sdk"] == version("mcp") and facts["loop"], facts
            boundary = result.details["boundary"]
            assert boundary["payload_started"] is True and boundary["provider"] == "sandbox", boundary
            assert boundary["requested"]["network_allowed"] is network_allowed, boundary
            observed[mode] = {"status": "supported", **facts}
            if mode == "workspace-edit":
                assert (project / "sdk-written.txt").read_text() == "official SDK"
        assert launches == [{"provider": "sandbox", "network_allowed": False},
                            {"provider": "sandbox", "network_allowed": True}], launches
        status = "supported" if all(item["status"] == "supported" for item in observed.values()) else "runtime_limited"
        return {"status": status, "sdk": version("mcp"), "modes": observed, "launches": launches}


def local_mcp_boundary(native, project, monkeypatch):
    extension = native.paths.user / "extensions" / "mcp.py"
    extension.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).resolve().parents[2] / "extensions" / "mcp.py", extension)
    sdk_status = _official_sdk(native, project, extension, monkeypatch)
    native.services["execution"].select_mode("workspace-edit")
    server = project / "mcp_server.py"
    shutil.copy(Path(__file__).with_name("mcp_boundary_server.py"), server)
    monkeypatch.setenv("MCP_AMBIENT_SECRET", "must-not-inherit")
    (native.paths.user / "mcp.json").write_text(json.dumps({"mcpServers": {"boundary": {
        "command": sys.executable, "args": ["-I", "-S", str(server)],
        "env": {"MCP_TEST_CREDENTIAL": "approved"},
    }}}), encoding="utf-8")
    native.prompt("/mcp reconnect")
    name = "mcp__boundary__probe"
    tool = native.tool(name)
    status = native.tool("mcp_status").execute({}, ToolContext("status", project, threading.Event()))
    assert tool is not None, status.as_text()
    context = ToolContext("workspace", project, threading.Event(), origin=native.main.origin)
    allowed = tool.execute({"path": "mcp-allowed.txt"}, context)
    assert not allowed.is_error, allowed.as_text()
    facts = json.loads(allowed.as_text())
    assert facts["credential"] == "approved" and facts["ambient_secret"] is None
    assert allowed.details["boundary"]["payload_started"] is True
    assert (project / "mcp-allowed.txt").read_text() == "written"
    outside = project.parent / "mcp-private.txt"
    outside.write_text("unchanged", encoding="utf-8")
    assert tool.execute({"path": str(outside)}, context).is_error
    assert outside.read_text() == "unchanged"

    native.services["execution"].select_mode("read-only")
    context = ToolContext("readonly", project, threading.Event(), origin=native.main.origin)
    assert tool.execute({"path": "mcp-blocked.txt"}, context).is_error
    assert not (project / "mcp-blocked.txt").exists()
    operation = {"path": str(outside), "content": "approved once"}
    native.ui.answers.append("y")
    before = len(tool_results(native))
    faux.script("test", faux.reply(tools=[("request_permissions", {
        "tool": name, "arguments": operation, "permissions": {"write_paths": [str(outside)]},
        "reason": "Verify a single MCP write grant.",
    })]), faux.reply(tools=[(name, operation)]), faux.reply(tools=[(name, operation)]), faux.reply("done"))
    native.prompt("Exercise the exact MCP operation grant and its expiry")
    approval, once, denied = tool_results(native)[before:]
    assert not approval.is_error, approval.text()
    assert not once.is_error and once.details["temporary_grant"], once.text()
    assert once.details["boundary"]["cleanup"] == "executor_completed"
    assert denied.is_error and outside.read_text() == "approved once", denied.text()

    api = ExtensionAPI(native, "mcp-origin")
    child = api.create_runtime("mcp-readonly-child", "subagent", parent=native.main)
    native.services["execution"].select_mode("no-limit")
    try:
        context = ToolContext("child", project, threading.Event(), agent=child.name, origin=child.origin)
        denied = tool.execute({"path": "mcp-child-blocked.txt"}, context)
        assert denied.is_error, denied.as_text()
        assert not (project / "mcp-child-blocked.txt").exists()
    finally:
        api.release_runtime(child)
    return sdk_status
