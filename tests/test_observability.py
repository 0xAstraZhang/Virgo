from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from virgo import ai
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, Usage, UserMessage
from virgo.harness.session import Runtime
from virgo.harness.store import SessionStore

from .conftest import RecordingUI
from .test_permission import event


def test_auxiliary_usage_survives_reload_without_entering_model_context(make_session, monkeypatch):
    session = make_session(persist=True)
    session.store.append_message(UserMessage("request"))
    session.store.append_message(AssistantMessage([TextContent("answer")], usage=Usage(10, 20, 30, 40)))
    before = session.store.build_context()
    reply = AssistantMessage([TextContent("private internal reply")], usage=Usage(1, 2, 3, 4))
    monkeypatch.setattr(ai, "complete", lambda *a, **kw: reply)
    session.complete("goal", "private system", "private prompt", effort="medium")
    restored = SessionStore.load(session.store.path)
    assert restored.total_usage() == dict(input=11, output=22, cache_read=33, cache_write=44)
    assert restored.build_context() == before
    raw = session.store.path.read_text()
    assert all(secret not in raw for secret in ("private system", "private prompt", "private internal reply"))
    session.new_session()
    assert session.store.total_usage() == dict(input=0, output=0, cache_read=0, cache_write=0)


def test_parallel_auxiliary_calls_are_counted_once(make_session, monkeypatch):
    session = make_session(persist=True)
    monkeypatch.setattr(ai, "complete", lambda *a, **kw: AssistantMessage(usage=Usage(1, 2, 3, 4)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: session.complete("workflow", "", ""), range(20)))
    restored = SessionStore.load(session.store.path)
    assert len(restored.custom("model_call")) == 20
    assert restored.total_usage() == dict(input=20, output=40, cache_read=60, cache_write=80)
    assert len(restored.branch()) == 20


def test_retry_usage_includes_failed_attempt_without_double_counting_final(make_session):
    session = make_session(faux.error("temporary", kind="retryable"), faux.reply("done"))
    session.prompt("work")
    records = session.store.custom("model_call")
    assert len(records) == 1 and records[0]["purpose"] == "retry"
    final = session.main.messages[-1]
    assert session.store.total_usage()["input"] == records[0]["usage"]["input"] + final.usage.input


def test_subagent_usage_reaches_parent_without_copying_child_messages(make_session):
    session = make_session()
    child = Runtime(session, "worker", "subagent", tools=[])
    child.agent.prompt([UserMessage("hello")])
    assert not session.main.messages
    assert session.store.build_context()[0] == []
    assert session.store.total_usage()["input"] == child.messages[-1].usage.input
    assert session.store.custom("model_call")[0]["agent"] == "worker"


def test_aborted_summary_keeps_context_and_counts_usage(make_session):
    session = make_session(settings={"compaction": {"keepRecentTokens": 1}})
    session.main.messages[:] = [UserMessage("request"), AssistantMessage([TextContent("text " * 200)])]
    before = copy.deepcopy(session.main.messages)
    faux.script("test", faux.reply("partial summary", stop_reason="aborted"))
    assert session.compact() is None
    assert session.main.messages == before
    assert session.store.custom("model_call")[0]["stop_reason"] == "aborted"
    session.abort()
    faux.script("test", faux.reply("complete summary"))
    assert session.compact() is not None
    assert not session.main.agent.signal.is_set()


@pytest.mark.parametrize("reply,answer,source,decision", [
    (faux.reply('{"safe":true,"reason":"public download"}'), None, "auto-review", "allow"),
    (faux.reply('{"safe":false,"reason":"unknown script"}'), "n", "auto-review", "deny"),
    (faux.reply("invalid"), "y", "review-error", "allow"),
    (faux.error("unavailable"), None, "review-error", "deny"),
])
def test_permission_audit_records_review_and_final_decision(make_session, tmp_path, reply, answer, source, decision):
    ui = RecordingUI(answers=[answer])
    session = make_session(ui=ui, mode="auto-review", persist=True)
    session.small_model = session.resolve("faux/reviewer")
    faux.script("reviewer", reply)
    ev = event(session, "write", {"path": str(tmp_path / "outside"), "content": "private payload"})
    permissions = session.services["permissions"]
    permissions.on_tool_call(ev, session)
    audit = session.store.custom("permission_decision")
    assert audit[0]["source"] == source
    assert audit[-1]["decision"] == decision
    assert audit[0]["model"] == "faux/reviewer"
    assert audit[0]["tool_call_id"] == ev.call_id
    assert audit[0]["reason"]
    assert audit[0]["duration"] >= 0
    calls = session.store.custom("model_call")
    assert len(calls) == 1 and calls[0]["tool_call_id"] == ev.call_id
    assert calls[0]["usage"]["input"] > 0
    assert "private payload" not in session.store.path.read_text()
    if decision == "allow":
        snapshot = copy.deepcopy(session.store.entries)
        assert permissions.before_execute(ev) is None
        assert session.store.entries == snapshot


def test_revalidation_has_auditable_argument_change(make_session, tmp_path):
    session = make_session(mode="auto-review")
    session.small_model = session.resolve("faux/reviewer")
    faux.script("reviewer", faux.reply('{"safe":true,"reason":"known output"}'))
    ev = event(session, "write", {"path": "inside", "content": "one"})
    perms = session.services["permissions"]
    perms.on_tool_call(ev, session)
    ev.args["path"] = str(tmp_path / "outside")
    perms.before_execute(ev)
    decisions = session.store.custom("permission_decision")
    assert [d["source"] for d in decisions] == ["workspace", "auto-review"]
    assert decisions[0]["arguments_sha256"] != decisions[1]["arguments_sha256"]
