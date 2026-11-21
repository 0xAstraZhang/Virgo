"""Session grants bind to (conversation, mode, path prefix) without brittle arg/inode hashes."""

from __future__ import annotations

import threading

import pytest

from virgo.agent.types import Block, ToolCallEvent, ToolContext

from tests.features.test_permission import request
from tests.support.harness import RecordingUI, tool_call_event


@pytest.mark.parametrize("change", ["arguments", "environment", "run", "inode_replace", "cwd"])
def test_session_grant_survives_benign_context_and_inode_changes(
    broker_session, tmp_path, monkeypatch, change,
):
    session = broker_session(ui=RecordingUI(["y"]))
    session.main.prepare_run()
    target = tmp_path / "external.txt"
    target.write_text("original")
    args = {"path": str(target), "content": "ok"}
    assert not request(session, "write", args).is_error

    event = tool_call_event(session, "write", args.copy())
    event.signal = threading.Event()
    assert session._before_tool(event) is None
    if change == "arguments":
        event.args["content"] = "updated-content"
    elif change == "environment":
        monkeypatch.setenv("GRANT_ENV_CHANGED", "1")
    elif change == "run":
        session.main.prepare_run()
    elif change == "inode_replace":
        replacement = tmp_path / "replacement.txt"
        replacement.write_text("atomic-save")
        replacement.replace(target)
    elif change == "cwd":
        event.cwd = tmp_path
    assert session._before_tool_execute(event) is None
    # A second call in the same session also succeeds without additional prompts.
    assert session._before_tool_execute(event) is None
    assert len(session.ui.questions) == 1


@pytest.mark.parametrize("boundary", ["origin", "mode", "revoke", "sibling"])
def test_session_grant_does_not_leak_across_session_mode_revoke_or_sibling(
    broker_session, tmp_path, boundary,
):
    session = broker_session(ui=RecordingUI(["y"]))
    session.main.prepare_run()
    granted = tmp_path / "granted.txt"
    args = {"path": str(granted), "content": "ok"}
    assert not request(session, "write", args).is_error

    provider = session.services["execution"]
    check_path = str(granted)
    if boundary == "origin":
        session.new_session()
    elif boundary == "mode":
        provider.select_mode("read-only")
        session.main.prepare_run()
    elif boundary == "revoke":
        provider.revoke()
    elif boundary == "sibling":
        check_path = str(tmp_path / "unapproved-sibling.txt")

    event = tool_call_event(session, "write", {"path": check_path, "content": "blocked"})
    event.signal = threading.Event()
    assert isinstance(session._before_tool_execute(event), Block)
    assert len(session.ui.questions) == 1


def test_overlapping_dispatches_share_session_grants_cleanly(broker_session, tmp_path):
    session = broker_session(ui=RecordingUI(["y", "y"]))
    session.main.prepare_run()
    runtime = session.main
    provider = session.services["execution"]
    signal = threading.Event()
    paths = []
    for filename in ("one.txt", "two.txt"):
        path = (tmp_path / filename).resolve()
        paths.append(str(path))
        args = {"path": str(path)}
        assert not request(session, "read", args, permissions={"read_paths": [str(path)]}).is_error
        event = ToolCallEvent("label", "read", args, session.tool("read"), "main", session.cwd,
                              origin=runtime.origin, signal=signal)
        assert session._before_tool_execute(event) is None
    context = ToolContext("label", session.cwd, signal, origin=runtime.origin)
    policy = provider.for_context(context)
    assert set(paths).issubset(set(policy.resources.read_paths))
