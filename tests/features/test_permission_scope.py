"""Exact grants bind resources and caller state, without parsing shell intent."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from virgo.agent.types import Block, ToolCallEvent

from tests.features.test_permission import request
from tests.support.harness import RecordingUI, tool_call_event


@pytest.mark.parametrize(
    "change", ["arguments", "environment", "expiry", "run", "origin", "policy", "cancel", "resource", "tool", "cwd"]
)
def test_grant_cannot_be_reused_after_context_changes(broker_session, tmp_path, monkeypatch, change):
    session = broker_session(ui=RecordingUI(["y"]))
    session.main.prepare_run()
    args = {"path": str(tmp_path / "external.txt"), "content": "ok"}
    if change == "resource":
        (tmp_path / "external.txt").write_text("original")
    assert not request(session, "write", args).is_error
    event = tool_call_event(session, "write", args.copy())
    event.signal = threading.Event()
    assert session._before_tool(event) is None
    provider = session.services["execution"]
    if change == "arguments":
        event.args["content"] = "changed"
    elif change == "environment":
        monkeypatch.setenv("GRANT_ENV_CHANGED", "1")
    elif change == "expiry":
        provider.sandbox._pending = {k: replace(g, expires=0) for k, g in provider.sandbox._pending.items()}
    elif change == "run":
        session.main.prepare_run()
    elif change == "origin":
        session.new_session()
        event.origin = session.main.origin
    elif change == "policy":
        provider.revoke()
    elif change == "cancel":
        event.signal.set()
    elif change == "resource":
        (tmp_path / "replacement.txt").write_text("replacement")
        (tmp_path / "external.txt").unlink()
        (tmp_path / "replacement.txt").replace(tmp_path / "external.txt")
    elif change == "tool":
        event.tool = replace(event.tool)
    elif change == "cwd":
        event.cwd = tmp_path
    assert isinstance(session._before_tool_execute(event), Block)
    if change == "resource":
        assert (tmp_path / "external.txt").read_text() == "replacement"
    else:
        assert not (tmp_path / "external.txt").exists()


def test_grant_is_atomically_consumed_once(broker_session, tmp_path):
    session = broker_session(ui=RecordingUI(["y"]))
    args = {"path": str(tmp_path / "external.txt"), "content": "ok"}
    assert not request(session, "write", args).is_error
    events = [
        ToolCallEvent(
            str(i),
            "write",
            args.copy(),
            session.tool("write"),
            "main",
            session.cwd,
            origin=session.main.origin,
        )
        for i in range(2)
    ]
    assert all(session._before_tool(e) is None for e in events)
    barrier = threading.Barrier(2)
    results = []

    def claim(event):
        barrier.wait()
        results.append(session._before_tool_execute(event))

    threads = [threading.Thread(target=claim, args=(e,)) for e in events]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r is None for r in results) == 1
    assert sum(isinstance(r, Block) for r in results) == 1
