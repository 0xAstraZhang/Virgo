"""MCP startup, transport and tool failures are diagnosable without hanging callers."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from virgo.agent.types import ToolContext
from virgo.features.mcp import STDERR_LIMIT, MCPClient, MCPError

from tests.support import SUPPORT


def test_startup_failure_records_stderr_and_exit_status(make_session):
    token = "mcp-private-credential-9876"
    script = (
        "import os,sys; "
        "sys.stderr.write('ModuleNotFoundError: missing_plugin; credential=' + os.environ['CUSTOM_VALUE']); "
        "sys.exit(23)"
    )
    session = make_session(settings={"mcpServers": {"broken": {
        "command": sys.executable, "args": ["-c", script], "env": {"CUSTOM_VALUE": token}, "timeout": 5,
    }}})
    client = session.services["mcp"]["broken"]
    assert client.error and client.error_details
    details = client.error_details
    assert details["context"]["server"] == "broken"
    assert details["context"]["method"] == "initialize"
    assert details["context"]["request_id"] == 1 and details["context"]["exit_code"] == 23
    assert "missing_plugin" in details["context"]["stderr_tail"]
    assert token not in json.dumps(details) and token not in session.ui.text
    assert "[redacted]" in details["context"]["stderr_tail"]
    assert details["error_id"] in session.ui.text
    assert session.tool("mcp__broken__echo") is None


def test_stderr_is_drained_and_keeps_only_a_bounded_tail():
    script = "import sys; sys.stderr.write('x' * 200000 + '\\nMissingDependency: install backend\\n'); sys.exit(7)"
    client = MCPClient("verbose", sys.executable, ["-c", script])
    try:
        with pytest.raises(MCPError) as caught:
            client.connect(timeout=5)
    finally:
        client.close()
    details = client.diagnostic(caught.value)
    assert len(client._stderr) <= STDERR_LIMIT
    assert details["context"]["stderr_truncated"] is True
    assert "MissingDependency: install backend" in details["context"]["stderr_tail"]
    assert client.proc.poll() == 7


def test_stdout_eof_wakes_request_even_while_server_is_still_alive():
    # An MCP server may close its protocol pipe while keeping another thread alive.
    # Waiting for process exit used to hold this request until its entire timeout.
    client = MCPClient("eof", sys.executable, ["-c", "import os,time; os.close(1); time.sleep(20)"])
    client.start()
    try:
        started = time.monotonic()
        with pytest.raises(MCPError, match="stdout") as caught:
            client.request("initialize", timeout=10)
        assert time.monotonic() - started < 2
        assert client.proc.poll() is None
        assert caught.value.method == "initialize" and caught.value.request_id == 1
        closed = time.monotonic()
        client.close()
        assert time.monotonic() - closed < 4
        assert client.proc.poll() is not None
    finally:
        client.close()


def test_reader_exception_wakes_request_with_the_original_cause(monkeypatch):
    release = threading.Event()

    class BrokenOutput:
        def __iter__(self):
            assert release.wait(2)
            raise OSError("MCP stdout read failed")

    client = MCPClient("reader", "unused")
    client.proc = SimpleNamespace(stdout=BrokenOutput(), poll=lambda: None)
    monkeypatch.setattr(client, "_send", lambda obj: release.set())
    worker = threading.Thread(target=client._reader)
    worker.start()
    started = time.monotonic()
    with pytest.raises(MCPError) as caught:
        client.request("tools/call", timeout=10)
    worker.join(timeout=1)
    assert not worker.is_alive() and time.monotonic() - started < 2
    details = client.diagnostic(caught.value)
    assert details["phase"] == "mcp.read"
    assert "OSError" in json.dumps(details["cause_chain"])
    assert "MCP stdout read failed" in details["traceback"]


def test_broken_pipe_tool_error_keeps_original_session_and_call(make_session, project, monkeypatch):
    session = make_session(settings={"mcpServers": {"echo": {
        "command": sys.executable, "args": [str(SUPPORT / "mcp_echo_server.py")],
    }}})
    original_store = session.store
    source = session.main.review_source
    client = session.services["mcp"]["echo"]
    tool = session.tool("mcp__echo__echo")

    def break_write(obj):
        session.new_session()
        raise BrokenPipeError("MCP stdin pipe broken")

    monkeypatch.setattr(client, "_send", break_write)
    result = tool.execute(
        {"text": "private-tool-input-not-for-diagnostics"},
        ToolContext("call-mcp-123", project, threading.Event(), review_source=source),
    )
    assert result.is_error and session.store is not original_store
    details = result.details["error"]
    assert details["phase"] == "mcp.write"
    assert details["context"]["session_id"] == original_store.id
    assert details["context"]["tool_call_id"] == "call-mcp-123"
    assert details["context"]["tool_name"] == "mcp__echo__echo"
    assert details["context"]["server"] == "echo" and details["context"]["method"] == "tools/call"
    assert details["context"]["request_id"] == 3
    assert "BrokenPipeError" in json.dumps(details["cause_chain"])
    assert "MCP stdin pipe broken" in details["traceback"]
    assert "private-tool-input-not-for-diagnostics" not in json.dumps(details)


def test_rpc_error_keeps_code_and_id_but_omits_error_data(monkeypatch):
    client = MCPClient("rpc", "unused")
    client.proc = SimpleNamespace(poll=lambda: None)

    def answer(obj):
        with client._cv:
            client._pending[obj["id"]] = {
                "error": {"code": -32603, "message": "backend unavailable", "data": "private-server-payload"},
            }
            client._cv.notify_all()

    monkeypatch.setattr(client, "_send", answer)
    with pytest.raises(MCPError) as caught:
        client.call("remote_tool", {"private": "tool-input"})
    details = client.diagnostic(caught.value)
    assert details["phase"] == "mcp.response"
    assert details["context"]["rpc_code"] == -32603 and details["context"]["request_id"] == 1
    assert details["context"]["method"] == "tools/call"
    assert "private-server-payload" not in json.dumps(details) and "tool-input" not in json.dumps(details)
    assert not client._waiting and not client._pending


def test_protocol_noise_is_reported_without_saving_raw_stdout():
    client = MCPClient("noise", "unused")
    client.proc = SimpleNamespace(stdout=io.BytesIO(b"invalid-json private-output\n"), poll=lambda: None)
    client._reader()
    with pytest.raises(MCPError) as caught:
        client.request("initialize", timeout=10)
    details = client.diagnostic(caught.value)
    assert "Expecting value" in details["context"]["protocol_error"]
    assert "private-output" not in json.dumps(details)
