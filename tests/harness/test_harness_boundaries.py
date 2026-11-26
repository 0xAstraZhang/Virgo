"""Real-session regressions for persistence, policy dispatch and candidate construction.

Failure cases: open/write/partial-write/close/serialization failure; a recovered
filesystem must not resume a failed store; results and end events must survive;
queues and independent sessions must remain usable. Policy errors must fail
closed without changing priority/snapshot order. Candidate callbacks must see
paired active state and explicit target ownership, including failed preparation.
"""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, ToolResultMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionStore

from tests.support.harness import calls


@pytest.mark.parametrize("failure", ["open", "write", "partial", "close", "serialize"])
def test_failed_save_stops_requests_and_keeps_history(make_session, monkeypatch, failure):
    s = make_session(faux.reply("must not run"), persist=True)
    original = builtins.open
    dumps = json.dumps
    target = s.store.path

    class BrokenFile:
        def __enter__(self):
            self.file = original(target, "a+b")
            return self

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, text):
            if failure == "partial":
                self.file.write(text[:10])
            elif failure == "close":
                return self.file.write(text)
            raise OSError("disk unavailable")

        def __exit__(self, *args):
            self.file.close()
            if failure == "close":
                raise OSError("flush unavailable")

    def open_file(path, *args, **kwargs):
        if Path(path) == target and args[0] == "a+b":
            if failure == "open":
                raise OSError("disk unavailable")
            return BrokenFile()
        return original(path, *args, **kwargs)

    def serialize(obj, *args, **kwargs):
        if isinstance(obj, dict) and obj.get("kind") == "transcript_run_start":
            raise ValueError("serialization unavailable")
        return dumps(obj, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "serialize":
            patch.setattr(json, "dumps", serialize)
        else:
            patch.setattr(builtins, "open", open_file)
        s.prompt("keep this input")
    saved = target.read_bytes() if target.exists() else b""
    s.prompt("keep the next input")
    s.notify("queued notification")
    assert not s.run_pending()
    assert not calls()
    assert "unsaved" in s.ui.text.lower()
    assert any(e.type == "agent_end" for e in s.ui.events)
    assert len(s.main.ids) == len(s.main.messages)
    assert target.read_bytes() == saved if target.exists() else not saved
    retained = [m.text() for m in s.main.messages] + [m.text() for m in s.wakeups]
    assert "keep this input" in retained and "keep the next input" in retained
    s.wait_idle(timeout=0.1)
    old = s.main
    s.new_session()
    s.prompt("healthy session")
    assert s.main.agent.last_text == "must not run"
    assert old.store.save_error is not None


def test_result_save_failure_delivers_result_and_blocks_restart(make_session, monkeypatch, project):
    s = make_session(faux.reply(tools=[("write", {"path": "result.txt", "content": "done"})]),
                     faux.reply("must not run"), persist=True)
    original = builtins.open

    def fail_after_tool(path, *args, **kwargs):
        if Path(path) == s.store.path and (project / "result.txt").exists():
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_after_tool)
    s.prompt("write the file")
    assert (project / "result.txt").read_text() == "done"
    assert len(calls()) == 1
    assert any(isinstance(m, ToolResultMessage) for m in s.main.messages)
    assert len(s.main.ids) == len(s.main.messages)
    assert {e.type for e in s.ui.events} >= {"tool_execution_end", "agent_end"}
    assert "disk full" in s.ui.text


def test_explicit_policy_preserves_order_and_fails_closed(make_session, project, monkeypatch):
    s = make_session(faux.reply(tools=[("write", {"path": "blocked.txt", "content": "x"})]), faux.reply("done"))
    seen = []

    class Policy:
        def preflight(self, event):
            seen.append("policy")

        def before_execute(self, event):
            seen.append("execute")
            raise RuntimeError("policy failed")

    s.bus.on("tool_call", lambda *args: seen.append("high"), priority=200)
    policy = Policy()
    monkeypatch.setattr(s.services["execution"], "preflight", policy.preflight)
    monkeypatch.setattr(s.services["execution"], "before_execute", policy.before_execute)
    s.bus.on("tool_call", lambda *args: seen.append("equal"), priority=100)
    s.bus.on("tool_call", lambda *args: seen.append("low"))
    s.prompt("write")
    assert seen == ["high", "policy", "equal", "low", "execute"]
    assert not (project / "blocked.txt").exists()
    assert any(isinstance(m, ToolResultMessage) and "policy failed" in m.text() for m in s.main.messages)


def test_policy_dispatch_uses_one_handler_snapshot(make_session):
    s = make_session(faux.reply(tools=[("glob", {"pattern": "*"})]), faux.reply("done"))
    seen = []

    def high(*args):
        s.bus.on("tool_call", lambda *args: seen.append("new"), priority=0)
        seen.append("high")

    s.bus.on("tool_call", high, priority=200)
    s.prompt("inspect")
    assert seen == ["high"]


def test_candidate_context_and_failed_activation_keep_active_pair(make_session, project):
    s = make_session(faux.reply("old"))
    s.prompt("original")
    old_store, old_main = s.store, s.main
    target = SessionStore(None, project)
    target.append("custom", kind="target", data="new")
    seen = []
    api = ExtensionAPI(s, "candidate")

    def section(context):
        assert (s.store, s.main) == (old_store, old_main)
        seen.append(context.store.custom("target"))
        assert context.accounting_store is target
        return "Target: new"

    api.add_runtime_system_section("target", section)
    candidate = s._new_main(target)
    assert candidate.accounting_store is target
    assert "Target: new" in candidate.system and seen == [["new"]]
    assert (s.store, s.main) == (old_store, old_main)

    def fail(context):
        raise ValueError("candidate rejected")

    api.add_runtime_system_section("reject", fail)
    with pytest.raises(ValueError, match="candidate rejected"):
        s._activate_store(target)
    assert (s.store, s.main) == (old_store, old_main)
    assert target not in s._visited_stores


def test_generic_request_fallback_matcher_triggers_compaction_fallback_without_mutation(make_session, monkeypatch):
    from tests.support.harness import install_responses

    session = make_session(faux.reply("earlier work"))
    session.prompt("Build the parser")
    mutated = []
    api = ExtensionAPI(session, "custom_capability")
    api.add_request_fallback(
        "custom",
        lambda _rt, _model, _ctx, _failed: mutated.append("called") or "custom-fallback",
        matches=lambda _ctx, failed: failed.stop_reason == "error" and failed.error_kind == "custom_unsupported",
    )
    install_responses(
        monkeypatch,
        AssistantMessage(stop_reason="error", error_kind="custom_unsupported", error_message="capability rejected"),
        AssistantMessage([TextContent("## Goal & Constraints\nFallback summary")]),
    )
    result = session.compact()
    assert result is not None and result["strategy"] == "serialized"
    assert "Fallback summary" in result["summary"]
    assert not mutated


def test_diagnostic_query_declaration_rolls_back_on_plugin_failure(make_session):
    session = make_session(settings={"plugins": {"diagnostic_get": {"enabled": False}}})
    assert session.main.diagnostic_query_tool_name() is None
    api = ExtensionAPI(session, "custom_diag")
    tool = AgentTool("custom_diag_query", "diag", {"type": "object"}, lambda _a, _c: "{}")
    api.register_tool(tool, roles=("main",))
    api.declare_diagnostic_query_tool("custom_diag_query")
    session.main.refresh_tools()
    assert session.main.diagnostic_query_tool_name() == "custom_diag_query"
    api.rollback()
    session.main.refresh_tools()
    assert session.main.diagnostic_query_tool_name() is None


def test_background_capability_toggle_updates_shell_tool_and_session_facade(make_session):
    from tests.support.shell import selected

    session = make_session()
    shell_name = selected(session).tool_name
    assert "background" in session.tool(shell_name).parameters["properties"]
    session.services["jobs"].set_background_enabled(False)
    assert "background" not in session.tool(shell_name).parameters["properties"]
    session.services["jobs"].set_background_enabled(True)
    assert "background" in session.tool(shell_name).parameters["properties"]

    seen = []
    unsub = session.observe_events(on_session_start=lambda s: seen.append(s.store.id))
    session.new_session()
    assert len(seen) == 1
    unsub()
    session.new_session()
    assert len(seen) == 1
