"""MCP recovery preserves authorization, transport uncertainty and operation identity."""

from __future__ import annotations

import json
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from virgo.agent.types import AgentTool, ToolContext
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.features.mcp import MAX_RECONNECT_ATTEMPTS, MCPClient, MCPError

from tests.support import SUPPORT
from tests.support.harness import calls

SERVER = SUPPORT / "mcp_echo_server.py"


def settings(*names):
    return {"mcpServers": {
        name: {"command": sys.executable, "args": [str(SERVER)], "timeout": 5} for name in names
    }}


def context(session, call_id="mcp-recovery", *, runtime=None):
    runtime = runtime or session.main
    return ToolContext(call_id, runtime.cwd, runtime.agent.signal, runtime.name, review_source=runtime.review_source)


def incident(session, name="echo", *, runtime=None):
    runtime = runtime or session.main
    client = session.services["mcp"][name]
    error = client.diagnostic(MCPError("connection failed", phase="mcp.read"))
    record = runtime.report_error(error, phase="mcp.read", notify=False)
    runtime.diagnostic_access.remember(record)
    return record


def reconnect(session, error, *, server="echo", ctx=None):
    return session.tool("mcp_reconnect").execute(
        {"server": server, "error_id": error["error_id"]}, ctx or context(session),
    )


def test_reconnect_scope_server_generation_and_stable_tool_binding(make_session):
    session = make_session(settings=settings("echo", "other"))
    original_runtime = session.main
    original = session.services["mcp"]["echo"]
    other = session.services["mcp"]["other"]
    wrapper = session.tool("mcp__echo__echo")
    error = incident(session)
    assert reconnect(session, error, server="other").is_error
    assert reconnect(session, {"error_id": "not-authorized"}).is_error
    assert session.services["mcp"]["echo"] is original

    session.new_session()
    assert reconnect(session, error).is_error
    # UI selection cannot remove authority from the runtime that owns the call.
    owner = context(session, runtime=original_runtime)
    result = reconnect(session, error, ctx=owner)
    assert not result.is_error and result.details["mcp_recovery"]["replayed_calls"] == 0
    replacement = session.services["mcp"]["echo"]
    assert replacement is not original and replacement.generation == original.generation + 1
    assert session.services["mcp"]["other"] is other and not other._closing
    assert session.tool("mcp__echo__echo") is wrapper
    assert wrapper.execute({"text": "new connection"}, owner).as_text() == "new connection"
    assert original._closing
    assert reconnect(session, error, ctx=owner).is_error
    assert session.services["mcp"]["echo"] is replacement


def test_status_is_local_and_does_not_expose_ungranted_error_ids(make_session, monkeypatch):
    session = make_session(settings=settings("echo"))
    client = session.services["mcp"]["echo"]
    client.error_details = incident(session)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("status must not contact the server")

    monkeypatch.setattr(client, "request", unexpected_request)
    own = json.loads(session.tool("mcp_status").execute({"server": "echo"}, context(session)).as_text())
    assert own["servers"][0]["error_id"] == client.error_details["error_id"]
    unbound = ToolContext("unbound", session.cwd, threading.Event())
    assert session.tool("mcp_status").execute({"server": "echo"}, unbound).is_error


def test_recovery_and_status_reject_foreign_or_forged_runtime_before_lookup(make_session, monkeypatch):
    owner = make_session(settings=settings("echo"))
    foreign = make_session(settings=settings("echo"))
    first = owner.services["mcp"]["echo"]
    foreign_client = foreign.services["mcp"]["echo"]
    error = incident(foreign)

    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("an untrusted scope must not be queried")

    monkeypatch.setattr(foreign.main.diagnostic_access, "get", forbidden_lookup)
    monkeypatch.setattr(owner.main.diagnostic_access, "get", forbidden_lookup)
    forged = context(owner)
    forged.review_source = SimpleNamespace(runtime=owner.main)
    wrong_agent = context(owner)
    wrong_agent.agent = "another-agent"
    for untrusted in (context(foreign), forged, wrong_agent):
        assert reconnect(owner, error, ctx=untrusted).is_error
        assert owner.tool("mcp_status").execute({"server": "echo"}, untrusted).is_error
    assert owner.services["mcp"]["echo"] is first and not first._closing
    assert foreign.services["mcp"]["echo"] is foreign_client and not foreign_client._closing


def test_inflight_reconnect_is_refused_without_spending_budget(make_session, monkeypatch):
    session = make_session(settings=settings("echo"))
    client = session.services["mcp"]["echo"]
    error = incident(session)
    entered, release = threading.Event(), threading.Event()
    original_call = client.call

    def held_call(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_call(*args, **kwargs)

    monkeypatch.setattr(client, "call", held_call)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(session.tool("mcp__echo__echo").execute, {"text": "running"}, context(session, "remote"))
        try:
            assert entered.wait(5)
            refused = reconnect(session, error)
            assert refused.is_error and "in-flight" in refused.as_text()
            assert refused.details["transport"]["dispatch"] == "not_started"
            status = json.loads(session.tool("mcp_status").execute({}, context(session)).as_text())["servers"][0]
            assert status["inflight"] == 1
            assert status["reconnect_attempts_remaining"] == MAX_RECONNECT_ATTEMPTS
            assert session.services["mcp"]["echo"] is client
        finally:
            release.set()
        assert running.result(timeout=5).as_text() == "running"


def test_concurrent_recovery_starts_only_one_candidate(make_session, monkeypatch):
    session = make_session(settings=settings("echo", "other"))
    error = incident(session)
    entered, release = threading.Event(), threading.Event()
    original_connect = MCPClient.connect
    candidates = []

    def held_connect(client, *args, **kwargs):
        candidates.append(client.name)
        entered.set()
        assert release.wait(5)
        return original_connect(client, *args, **kwargs)

    monkeypatch.setattr(MCPClient, "connect", held_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(reconnect, session, error)
        try:
            assert entered.wait(5)
            refused = reconnect(session, error)
            assert refused.is_error and "already pending" in refused.as_text()
            assert session.tool("mcp__other__echo").execute({"text": "independent"}, context(session)).as_text() == "independent"
        finally:
            release.set()
        assert not running.result(timeout=5).is_error
    assert candidates == ["echo"]


def test_failed_candidates_preserve_original_and_bound_recovery(make_session, monkeypatch):
    session = make_session(settings=settings("echo"))
    original = session.services["mcp"]["echo"]
    error = incident(session)
    attempts = []

    def broken_connect(client, *args, **kwargs):
        attempts.append(client)
        raise OSError("dependency is still unavailable")

    monkeypatch.setattr(MCPClient, "connect", broken_connect)
    for _ in range(MAX_RECONNECT_ATTEMPTS):
        result = reconnect(session, error)
        assert result.is_error
        details = result.details["error"]
        assert details["context"]["generation"] == original.generation
        assert details["context"]["candidate_generation"] == original.generation + 1
        assert session.main.diagnostic_access.get(details["error_id"])
        assert session.services["mcp"]["echo"] is original and not original._closing
    exhausted = reconnect(session, error)
    assert exhausted.is_error and "budget" in exhausted.as_text()
    assert len(attempts) == MAX_RECONNECT_ATTEMPTS
    assert session.tool("mcp__echo__echo").execute({"text": "progress"}, context(session)).as_text() == "progress"
    status = json.loads(session.tool("mcp_status").execute({}, context(session)).as_text())["servers"][0]
    assert status["reconnect_attempts_remaining"] == MAX_RECONNECT_ATTEMPTS


def test_handshake_alone_does_not_reset_recovery_budget(make_session):
    session = make_session(settings=settings("echo"))
    for _ in range(MAX_RECONNECT_ATTEMPTS):
        assert not reconnect(session, incident(session)).is_error
    result = reconnect(session, incident(session))
    assert result.is_error and "budget" in result.as_text()


def test_changed_schema_is_published_at_next_model_boundary(make_session, monkeypatch):
    session = make_session(faux.reply("continued"), settings=settings("echo"))
    original_client = session.services["mcp"]["echo"]
    original_wrapper = session.tool("mcp__echo__echo")
    original_connect = MCPClient.connect

    def changed_connect(client, *args, **kwargs):
        original_connect(client, *args, **kwargs)
        client.tools[0]["inputSchema"]["properties"]["format"] = {"type": "string"}

    monkeypatch.setattr(MCPClient, "connect", changed_connect)
    recovered = reconnect(session, incident(session))
    assert recovered.details["mcp_recovery"]["refresh_required"]
    assert session.services["mcp"]["echo"] is original_client
    assert session.tool("mcp__echo__echo") is original_wrapper
    pending_call = original_wrapper.execute({"text": "not sent"}, context(session))
    assert pending_call.is_error and pending_call.details["transport"]["dispatch"] == "not_started"
    session.prompt("continue")
    replacement = session.services["mcp"]["echo"]
    assert replacement is not original_client and original_client._closing
    new_wrapper = session.tool("mcp__echo__echo")
    assert new_wrapper is not original_wrapper and new_wrapper in session.main.tools
    stale_call = original_wrapper.execute({"text": "old schema"}, context(session))
    assert stale_call.is_error and "schema changed" in stale_call.as_text()
    assert new_wrapper.execute({"text": "updated schema"}, context(session)).as_text() == "updated schema"


def test_startup_failure_can_recover_missing_tools_at_boundary(make_session, monkeypatch):
    original_connect = MCPClient.connect
    attempted = set()

    def fail_once(client, *args, **kwargs):
        if client.name not in attempted:
            attempted.add(client.name)
            raise OSError("dependency unavailable at startup")
        return original_connect(client, *args, **kwargs)

    monkeypatch.setattr(MCPClient, "connect", fail_once)
    session = make_session(faux.reply("continued"), settings=settings("echo"))
    error = session.services["mcp"]["echo"].error_details
    assert session.main.diagnostic_access.get(error["error_id"])
    assert session.tool("mcp__echo__echo") is None
    result = reconnect(session, error)
    assert not result.is_error and result.details["mcp_recovery"]["refresh_required"]
    assert session.tool("mcp__echo__echo") is None
    session.prompt("continue")
    assert "mcp__echo__echo" in calls()[-1]["tools"]
    assert session.tool("mcp__echo__echo").execute({"text": "repaired"}, context(session)).as_text() == "repaired"


@pytest.mark.parametrize("invalid", [
    {"description": 42}, {"inputSchema": []}, {"inputSchema": {"properties": []}},
    {"inputSchema": {"required": 42}}, {"inputSchema": {"properties": {"x": {"items": []}}}},
    {"inputSchema": {"type": "array"}},
])
def test_invalid_candidate_schema_keeps_original_registry_and_connection(make_session, monkeypatch, invalid):
    session = make_session(faux.reply("continued"), settings=settings("echo"))
    original = session.services["mcp"]["echo"]
    wrapper = session.tool("mcp__echo__echo")
    connect = MCPClient.connect
    candidates = []

    def malformed(client, *args, **kwargs):
        connect(client, *args, **kwargs)
        candidates.append(client)
        client.tools[0].update(invalid)

    monkeypatch.setattr(MCPClient, "connect", malformed)
    result = reconnect(session, incident(session))
    assert result.is_error and result.details["error"]["phase"] == "mcp.schema"
    assert session.main.diagnostic_access.get(result.details["error"]["error_id"])
    assert session.services["mcp"]["echo"] is original and not original._closing
    assert session.tool("mcp__echo__echo") is wrapper
    assert candidates[0]._closing and candidates[0].proc.poll() is not None
    session.prompt("continue")
    assert session.main.agent.last_text == "continued"
    assert wrapper.execute({"text": "retained"}, context(session)).as_text() == "retained"


def test_late_registry_collision_discards_candidate_without_partial_refresh(make_session, monkeypatch):
    session = make_session(faux.reply("continued"), settings=settings("echo"))
    original = session.services["mcp"]["echo"]
    wrapper = session.tool("mcp__echo__echo")
    connect = MCPClient.connect
    candidates = []

    def expanded(client, *args, **kwargs):
        connect(client, *args, **kwargs)
        client.tools.append({"name": "new", "inputSchema": {"type": "object"}})
        candidates.append(client)

    monkeypatch.setattr(MCPClient, "connect", expanded)
    result = reconnect(session, incident(session))
    assert not result.is_error and result.details["mcp_recovery"]["refresh_required"]
    conflicting = AgentTool("mcp__echo__new", "extension tool", {"type": "object"}, lambda *_args: "extension")
    session.register_tool(conflicting)
    session.prompt("continue after extension change")
    assert session.main.agent.last_text == "continued"
    assert session.services["mcp"]["echo"] is original and not original._closing
    assert session.tool("mcp__echo__echo") is wrapper
    assert session.tool("mcp__echo__new") is conflicting
    assert candidates[0]._closing and candidates[0].proc.poll() is not None
    errors = [record for record in session.main.diagnostic_access.recent() if record["phase"] == "mcp.schema"]
    assert len(errors) == 1 and "conflicts" in errors[0]["message"]
    assert wrapper.execute({"text": "retained"}, context(session)).as_text() == "retained"


@pytest.mark.parametrize("repeated", [True, False])
def test_handshake_bounds_tools_list_pagination(monkeypatch, repeated):
    client = MCPClient("pages", "unused")
    requested = []
    monkeypatch.setattr(client, "start", lambda: None)
    monkeypatch.setattr(client, "notify", lambda *_args: None)
    monkeypatch.setattr("virgo.features.mcp.MAX_TOOL_PAGES", 2)

    def request(method, *args):
        requested.append(method)
        client._request_local.id = len(requested)
        if method == "initialize":
            return {}
        return {"tools": [], "nextCursor": "same" if repeated else str(len(requested))}

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(MCPError) as caught:
        client.connect(timeout=5)
    assert caught.value.phase == "mcp.protocol"
    assert requested == ["initialize", "tools/list", "tools/list"]


def test_handshake_uses_one_deadline_across_requests(monkeypatch):
    client = MCPClient("deadline", "unused")
    clock, requested = [0.0], []
    monkeypatch.setattr("virgo.features.mcp.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(client, "start", lambda: None)
    monkeypatch.setattr(client, "notify", lambda *_args: None)

    def request(method, *args):
        requested.append(method)
        clock[0] = 6.0
        return {}

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(MCPError) as caught:
        client.connect(timeout=5)
    assert caught.value.phase == "mcp.timeout" and requested == ["initialize"]


def test_lost_reply_preserves_unknown_effect_and_never_replays(make_session, project):
    marker = project / "effects.txt"
    server = project / "lost_reply.py"
    server.write_text(textwrap.dedent("""
        import json, sys
        from pathlib import Path
        for line in sys.stdin:
            req = json.loads(line)
            if "id" not in req:
                continue
            if req["method"] == "initialize":
                result = {"serverInfo": {"name": "loss"}}
            elif req["method"] == "tools/list":
                result = {"tools": [{"name": "mutate", "inputSchema": {"type": "object"}}]}
            elif req["method"] == "tools/call":
                with Path(sys.argv[1]).open("a") as output:
                    output.write("effect\\n")
                sys.stderr.write("response channel failed after mutation\\n")
                sys.stderr.flush()
                sys.exit(0)
            print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
    """), encoding="utf-8")
    session = make_session(settings={"mcpServers": {"loss": {
        "command": sys.executable, "args": [str(server), str(marker)], "timeout": 5,
    }}})
    result = session.tool("mcp__loss__mutate").execute({}, context(session))
    assert result.is_error
    assert result.details["transport"] == {
        "protocol": "mcp", "server": "loss", "generation": 1,
        "dispatch": "sent", "outcome": "unknown", "side_effects": "unknown",
    }
    assert marker.read_text().splitlines() == ["effect"]
    error = result.details["error"]
    assert error["context"]["generation"] == 1 and error["context"]["outcome"] == "unknown"
    recovered = reconnect(session, error, server="loss")
    assert not recovered.is_error and recovered.details["mcp_recovery"]["replayed_calls"] == 0
    assert marker.read_text().splitlines() == ["effect"]


def test_write_failure_cannot_claim_request_was_not_sent(make_session, monkeypatch):
    session = make_session(settings=settings("echo"))
    client = session.services["mcp"]["echo"]
    original_send = client._send

    def fail_after_write(obj):
        original_send(obj)
        raise BrokenPipeError("peer received data before local write failure")

    monkeypatch.setattr(client, "_send", fail_after_write)
    result = session.tool("mcp__echo__echo").execute({"text": "request"}, context(session))
    assert result.is_error
    assert result.details["transport"]["dispatch"] == "sending"
    assert result.details["transport"]["outcome"] == "unknown"
    assert result.details["transport"]["side_effects"] == "unknown"
    assert result.details["error"]["context"]["dispatch"] == "sending"
    client.close()
    not_started = session.tool("mcp__echo__echo").execute({"text": "cannot send"}, context(session))
    assert not_started.details["transport"]["dispatch"] == "not_started"
    assert not_started.details["transport"]["side_effects"] == "none"


def test_plan_permission_blocks_reconnection_before_process_start(make_session):
    session = make_session(mode="plan", settings=settings("echo"))
    error = incident(session)
    original = session.services["mcp"]["echo"]
    faux.script("test", faux.reply(tools=[("mcp_reconnect", {"server": "echo", "error_id": error["error_id"]})]),
                faux.reply("blocked"))
    session.prompt("inspect only")
    results = [message for message in session.main.messages if isinstance(message, ToolResultMessage)]
    assert results[0].is_error and "Plan mode" in results[0].text()
    assert session.services["mcp"]["echo"] is original and not original._closing
    assert not session.tool("mcp_reconnect").read_only and session.tool("mcp_status").read_only


def test_manual_reload_changes_generation_and_invalidates_old_incident(make_session):
    session = make_session(settings=settings("echo"))
    error = incident(session)
    generation = session.services["mcp"]["echo"].generation
    session.prompt("/mcp reconnect")
    current = session.services["mcp"]["echo"]
    assert current.generation > generation
    assert reconnect(session, error).is_error
    assert session.services["mcp"]["echo"] is current
