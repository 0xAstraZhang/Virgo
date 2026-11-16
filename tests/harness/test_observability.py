"""Usage and permission audits are recorded without entering the model context."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from virgo import ai
from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, Usage, UserMessage
from virgo.harness.session import Runtime
from virgo.harness.store import SessionStore

from tests.support.harness import RecordingUI, tool_call_event


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


@pytest.mark.parametrize("approval,answer,decision", [
    ("ask", "y", "allow"),
    ("ask", "n", "deny"),
    ("ask", "a", "allow"),
    ("never", None, "deny"),
], ids=[
    "reply0-None-auto-review-allow", "reply1-n-auto-review-deny",
    "reply2-y-review-error-allow", "reply3-None-review-error-deny",
])
def test_permission_audit_records_review_and_final_decision(make_session, tmp_path, approval, answer, decision):
    # Preserve the comparison identities while auditing explicit policy and human
    # decisions instead of the retired model reviewer.
    ui = RecordingUI(answers=[answer])
    session = make_session(ui=ui, allow=False, approval=approval, persist=True,
                           settings={"sandbox": {"mode": "workspace-write"}})
    # An unmanaged trusted extension retains explicit tool approval. Managed
    # file operations instead obey the selected profile at execution time.
    session.register_tool(AgentTool("external_write", "External write", {"type": "object"}, lambda *_: "done"))
    ev = tool_call_event(session, "external_write", {"path": str(tmp_path / "outside"), "content": "private payload"})
    permissions = session.services["permissions"]
    permissions.on_tool_call(ev, session)
    audit = session.store.custom("permission_decision")
    assert audit[0]["source"] == ("manual-approval" if approval == "ask" else "approval-never")
    assert audit[-1]["decision"] == decision
    assert audit[0]["approval_policy"] == approval
    assert audit[0]["work_mode"] == "default" and not audit[0]["read_only"]
    assert all("model" not in record for record in audit)
    assert audit[0]["tool_call_id"] == ev.call_id
    assert audit[0]["reason"]
    assert audit[0]["duration"] >= 0
    assert not session.store.custom("model_call")
    if approval == "ask":
        assert audit[-1]["source"] == "human" and len(audit) == 2
    else:
        assert len(audit) == 1 and not ui.questions
    assert "private payload" not in session.store.path.read_text()
    if decision == "allow":
        snapshot = copy.deepcopy(session.store.entries)
        assert permissions.before_execute(ev) is None
        assert session.store.entries == snapshot


def test_revalidation_has_auditable_argument_change(make_session, tmp_path):
    session = make_session(allow=False, ui=RecordingUI(answers=["y"]),
                           settings={"sandbox": {"mode": "workspace-write"}})
    ev = tool_call_event(session, "write", {"path": "inside", "content": "one"})
    perms = session.services["permissions"]
    perms.on_tool_call(ev, session)
    ev.args["path"] = str(tmp_path / "outside")
    assert perms.before_execute(ev) is not None
    decisions = session.store.custom("permission_decision")
    assert [d["source"] for d in decisions] == ["workspace", "execution-policy"]
    assert decisions[0]["arguments_sha256"] != decisions[1]["arguments_sha256"]
    assert decisions[1]["decision"] == "deny" and not session.ui.questions
