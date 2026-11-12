"""MCP servers: the client handshake, namespaced and permission-checked tools, and bad configs."""

from __future__ import annotations

import json
import sys
import textwrap
from importlib.metadata import version

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import (
    ToolResultMessage,
)
from virgo.features.mcp import MCPClient

from tests.support import SUPPORT
from tests.support.harness import RecordingUI, calls

SERVER = SUPPORT / "mcp_echo_server.py"


def test_mcp_client_handshake_and_calls(tmp_path):
    handshake = tmp_path / "initialize.json"
    c = MCPClient("echo", sys.executable, [str(SERVER), str(handshake)])
    c.connect(timeout=10)
    try:
        assert json.loads(handshake.read_text())["clientInfo"] == {
            "name": "virgo",
            "version": version("virgo"),
        }
        assert [t["name"] for t in c.tools] == ["echo", "add"]
        assert c.call("add", {"a": 2, "b": 3})["content"][0]["text"] == "5"
    finally:
        c.close()


def test_mcp_tools_are_namespaced_and_permission_checked(make_session, project, monkeypatch):
    reviewed = []

    def review(_session, event, _reason):
        reviewed.append(event.tool_name)
        return False, "external service needs approval"

    monkeypatch.setattr("virgo.features.permission.review_tool_call", review)
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"echo": {"command": sys.executable, "args": [str(SERVER)]}}})
    )
    ui = RecordingUI(answers=["y"])
    s = make_session(
        faux.reply(tools=[("mcp__echo__echo", {"text": "hello mcp"}), ("mcp__echo__add", {"a": 1, "b": 2})]),
        faux.reply("ok"),
        ui=ui,
        mode="auto-review",
        settings={"permissions": {"allow": ["mcp__echo__add"]}},
    )
    s.prompt("use mcp")
    assert "mcp__echo__echo" in calls()[0]["tools"]
    res = [m.text() for m in s.main.messages if isinstance(m, ToolResultMessage)]
    assert res == ["hello mcp", "3"]
    assert len(ui.questions) == 1  # echo asked; add allowed by rule
    assert reviewed == ["mcp__echo__echo"]
    assert s.main.agent.last_text == "ok"


def test_mcp_reconnect_rebinds_tools_to_the_new_connection(make_session, project):
    (project / ".virgo").mkdir()
    config = {"mcpServers": {"echo": {"command": sys.executable, "args": [str(SERVER)]}}}
    (project / ".virgo" / "mcp.json").write_text(json.dumps(config))
    ui = RecordingUI()
    s = make_session(
        faux.reply(tools=[("mcp__echo__add", {"a": 1, "b": 2})]),
        faux.reply("first"),
        faux.reply(tools=[("mcp__echo__add", {"a": 20, "b": 30})]),
        faux.reply("second"),
        ui=ui,
    )
    s.prompt("add")
    first = s.services["mcp"]["echo"]
    s.prompt("/mcp reconnect")
    assert s.services["mcp"]["echo"] is not first and "duplicate" not in ui.text
    s.prompt("add again")
    # The tools used to stay bound to the closed client: "ValueError: write to closed file".
    results = [(m.text(), m.is_error) for m in s.main.messages if isinstance(m, ToolResultMessage)]
    assert results == [("3", False), ("50", False)]
    assert [t.name for t in s.main.tools].count("mcp__echo__add") == 1

    # A server removed from the configuration takes its tools with it.
    (project / ".virgo" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    s.prompt("/mcp reconnect")
    assert not any(t.name.startswith("mcp__") for t in s.main.tools)
    assert s.tool("mcp__echo__add", "subagent") is None


def test_malformed_mcp_config_does_not_stop_the_session(make_session, project):
    (project / ".virgo").mkdir()
    (project / ".virgo" / "mcp.json").write_text("[]")
    session = make_session()
    assert session.services["mcp"] == {} and "mcp: invalid" in session.ui.text


@pytest.mark.parametrize("bad", [["x"], "abc", 5])
def test_malformed_mcp_servers_setting_does_not_stop_the_session(make_session, bad):
    session = make_session(settings={"mcpServers": bad})
    assert session.services["mcp"] == {}
    assert "mcp: invalid settings: mcpServers must be an object" in session.ui.text


def test_mcp_server_with_odd_output_cannot_stop_the_session(make_session, project):
    server = project / "odd_server.py"
    server.write_text(textwrap.dedent("""
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line)
            if "id" not in msg:
                continue
            print(42, flush=True)  # not a JSON-RPC object
            result = {"serverInfo": {}} if msg["method"] == "initialize" else {
                "tools": [{"name": "echo"}, {"description": "no name"}, "junk"]}
            if sys.argv[1] == "bad":
                result = "not an object"
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """))
    servers = {
        name: {"command": sys.executable, "args": [str(server), name], "timeout": 10} for name in ("odd", "bad")
    }
    session = make_session(settings={"mcpServers": servers})
    clients = session.services["mcp"]
    assert clients["odd"].error is None and [t["name"] for t in clients["odd"].tools] == ["echo"]
    assert session.tool("mcp__odd__echo") is not None
    assert "invalid result" in clients["bad"].error
