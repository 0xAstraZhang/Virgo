"""Exact grants bind resources and caller state, without parsing shell intent."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from virgo.agent.types import ToolCallEvent, ToolContext

from tests.features.test_permission import request
from tests.support.harness import RecordingUI, tool_call_event


@pytest.mark.parametrize(
    "change", ["arguments", "environment", "expiry", "run", "origin", "policy", "cancel", "tool", "cwd"]
)
def test_grant_cannot_be_reused_after_context_changes(broker_session, tmp_path, monkeypatch, change):
    session = broker_session(ui=RecordingUI(["y"]))
    session.main.prepare_run()
    args = {"path": str(tmp_path / "external.txt"), "content": "ok"}
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
    elif change == "tool":
        event.tool = replace(event.tool)
    elif change == "cwd":
        event.cwd = tmp_path
    assert session._before_tool_execute(event) is None
    effective = provider.for_context(event)
    assert not effective.policy.allows(tmp_path / "external.txt", "write")
    assert not (tmp_path / "external.txt").exists()


def test_resource_replacement_keeps_the_lexical_operation_grant(broker_session, tmp_path):
    session = broker_session(ui=RecordingUI(["y"]))
    target = tmp_path / "external.txt"
    target.write_text("original")
    args = {"path": str(target), "content": "approved"}
    assert not request(session, "write", args).is_error
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("replacement")
    replacement.replace(target)
    event = tool_call_event(session, "write", args)
    assert session._before_tool(event) is None
    assert session._before_tool_execute(event) is None
    effective = session.services["execution"].for_context(event)
    assert effective.policy.allows(target.parent, "write")
    assert target.read_text() == "replacement", "Approval must not perform the operation"


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
        assert session._before_tool_execute(event) is None
        results.append(session.services["execution"].for_context(event).policy.allows(Path(args["path"]), "write"))

    threads = [threading.Thread(target=claim, args=(e,)) for e in events]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]

def test_overlapping_dispatches_keep_their_context_when_call_labels_repeat(broker_session, tmp_path):
    session = broker_session(ui=RecordingUI(["y", "y"]))
    session.main.prepare_run()
    runtime = session.main
    provider = session.services["execution"]
    events = []
    signal = threading.Event()
    for filename in ("one.txt", "two.txt"):
        path = tmp_path / filename
        args = {"path": str(path)}
        assert not request(session, "read", args, permissions={"read_paths": [str(path)]}).is_error
        event = ToolCallEvent("label", "read", args, session.tool("read"), "main", session.cwd,
                              origin=runtime.origin, signal=signal)
        assert session._before_tool_execute(event) is None
        events.append(event)
    context = ToolContext("label", session.cwd, signal, origin=runtime.origin)
    assert provider.for_context(context) is runtime.execution_context
    for event in events:
        policy = provider.for_context(replace(context, dispatch_event=event))
        assert policy.resources.read_paths == (*runtime.execution_context.resources.read_paths, event.args["path"])


def test_lexical_path_tilde_normalization_and_invalid_request_paths(broker_session, tmp_path):
    from virgo.execution_policy import lexical_path
    from virgo.sandbox.platform import NativePolicy, SandboxUnavailable

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    assert lexical_path("~//nested/file.txt", tmp_path, home=fake_home) == fake_home / "nested" / "file.txt"
    assert lexical_path("~//", tmp_path, home=fake_home) == fake_home

    session = broker_session(ui=RecordingUI(["y"]))
    for bad in ("~other/secret.txt", "bad\0path"):
        res_perm = request(session, "read", {"path": str(tmp_path / "ok.txt")}, permissions={"read_paths": [bad]})
        assert res_perm.is_error
        res_arg = request(session, "read", {"path": bad}, permissions={"read_paths": [str(tmp_path)]})
        assert res_arg.is_error

    if os.name == "posix":
        workspace = tmp_path / "native-workspace"
        workspace.mkdir()
        enclosing = tmp_path / "enclosing"
        runtime_root = enclosing / "runtime"
        runtime_root.mkdir(parents=True)
        native = NativePolicy(workspace, True, False, (enclosing.resolve(),), (),
                              extra_runtime_paths=(runtime_root.resolve(),))
        with pytest.raises(SandboxUnavailable) as failure:
            native._linux(Path("/usr/bin/bwrap"), tmp_path, workspace.resolve(), 3, protected_mounts=())
        assert failure.value.reason == "unsupported_read_deny"
        assert failure.value.dimension == "filesystem_read_deny"
