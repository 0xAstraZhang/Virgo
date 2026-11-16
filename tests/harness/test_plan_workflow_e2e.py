"""Planning workflow acceptance, specified before production changes.

Failure cases: a proposal or Todo silently authorizes project writes; an edited
plan retains an earlier confirmation; a missing, stale, or foreign token starts
execution; explicit execution loses the exact plan or broadens approvals or the
sandbox; replay starts a run or restores a grant; a conversation switch lends its
mode or plan to an older runtime/child; startup defaults discard saved Plan; a
busy model request admits a second execution. These scenarios exercise assembled
sessions, the real tool loop, disk persistence, and actual file side effects.
"""
from __future__ import annotations

import threading

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionStore

from tests.support.harness import calls, tool_results
from tests.support.persistence import fail_session_write

PLAN = """# Create the answer file

## Goal
Record the requested answer in answer.txt.

## Scope
Only answer.txt changes; source.txt remains unchanged.

## Decisions
Use a UTF-8 text file with the exact content `first answer`.

## Steps
1. Read source.txt.
2. Write answer.txt after explicit execution confirmation.
3. Read the resulting file.

## Acceptance
answer.txt contains `first answer`; source.txt is unchanged.

## Constraints
Keep the current approval policy and sandbox restrictions.
""".strip()
REVISED_PLAN = PLAN.replace("first answer", "revised answer")


def propose(session, markdown=PLAN):
    faux.push("test", faux.reply(tools=[("propose_plan", {"markdown": markdown})]),
              faux.reply("The proposal is ready for your review."))
    session.prompt("Investigate the repository and propose a plan without executing it.")
    result = tool_results(session)[-1]
    assert result.tool_name == "propose_plan" and not result.is_error, result.text()
    return session.services["plans"].current


def test_proposal_is_a_separate_artifact_and_cannot_write_or_replace_todos(make_session, project):
    (project / "source.txt").write_text("existing source", encoding="utf-8")
    session = make_session(
        settings={"profile": "minimal", "workMode": "plan", "plugins": {"todo": {"enabled": True}}},
        persist=True, refresh_models=False, extensions=False,
    )
    faux.push("test", faux.reply(tools=[("read", {"path": "source.txt"})]),
              faux.reply(tools=[("propose_plan", {"markdown": PLAN})]),
              faux.reply(tools=[("write", {"path": "answer.txt", "content": "unauthorized"}),
                                ("todo_write", {"todos": [{"content": "Execute now", "status": "in_progress"}]})]),
              faux.reply("A proposed plan does not authorize execution."))
    session.prompt("Plan the change; do not implement it.")

    plan = session.services["plans"].current
    assert plan.markdown == PLAN and plan.revision == 1
    assert plan.plan_id and plan.token == f"{plan.plan_id}:1"
    assert plan.confirmed_revision is None
    assert session.services["permissions"].work_mode == "plan"
    assert session.permission_mode == "accept-all"
    assert (project / "source.txt").read_text(encoding="utf-8") == "existing source"
    assert not (project / "answer.txt").exists()
    assert not (project / "plan.md").exists()
    results = {result.tool_name: result for result in tool_results(session)}
    assert not results["read"].is_error and not results["propose_plan"].is_error
    assert results["write"].is_error and results["todo_write"].is_error
    assert session.services["todos"]["todos"] == []
    for request in calls():
        assert "propose_plan" in request["tools"]
        assert not {"write", "edit", "todo_write"}.intersection(request["tools"])
    count = len(calls())
    session.prompt("/plan")
    session.prompt("/plan show")
    assert PLAN in session.ui.text and plan.token in session.ui.text
    assert len(calls()) == count
    assert SessionStore.load(session.store.path).entries


def test_revision_invalidates_confirmation_and_only_exact_explicit_execution_runs(make_session, project):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    first = propose(session)
    faux.push("test", faux.reply("Execution started with the selected plan."))
    session.prompt(f"/plan execute {first.token}")
    assert session.services["plans"].current.confirmed_revision == first.revision
    assert session.services["permissions"].work_mode == "default"

    count = len(calls())
    session.prompt(f"/plan edit {first.token}\n{REVISED_PLAN}")
    revised = session.services["plans"].current
    assert revised.plan_id == first.plan_id and revised.revision == first.revision + 1
    assert revised.markdown == REVISED_PLAN and revised.confirmed_revision is None
    assert session.services["permissions"].work_mode == "plan"
    session.prompt(f"/plan edit {first.token}\n{PLAN}")
    assert session.services["plans"].current == revised, "A stale editor must not overwrite the new revision"
    session.prompt(f"/plan execute {first.token}")
    session.prompt("/plan execute")
    session.prompt("/mode default")
    assert len(calls()) == count and not session.run_pending()
    assert session.services["plans"].current.confirmed_revision is None
    assert not (project / "answer.txt").exists()

    session.prompt("/mode plan")
    faux.push("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "revised answer"})]),
              faux.reply("Implemented the confirmed revision."))
    session.prompt(f"/plan execute {revised.token}")
    assert (project / "answer.txt").read_text(encoding="utf-8") == "revised answer"
    assert not tool_results(session)[-1].is_error
    assert session.services["permissions"].work_mode == "default"
    assert session.permission_mode == "accept-all"
    assert session.services["plans"].current.confirmed_revision == revised.revision
    request = calls()[count]
    messages = [message for message in session.main.messages if isinstance(message, UserMessage)]
    assert REVISED_PLAN in messages[-1].text()
    assert REVISED_PLAN in str(request["messages"][-1]["content"])
    assert "write" in request["tools"] and "propose_plan" not in request["tools"]


@pytest.mark.parametrize("conditional", [False, True])
def test_reentrant_mode_edit_preserves_version_bound_execution(make_session, conditional):
    session = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    original = propose(session)
    session.prompt("/mode default")
    published = []
    session.bus.on("proposed_plan_changed", lambda _runtime, plan: published.append(plan))

    def revise_on_plan(_runtime, mode):
        if mode == "plan":
            session.prompt(f"/plan edit {original.token}\n{REVISED_PLAN}")

    unsubscribe = session.bus.on("work_mode_changed", revise_on_plan)
    count = len(calls())
    command = f"/plan edit {original.token}\n{PLAN}" if conditional else f"/plan edit {PLAN}"
    session.prompt(command)
    unsubscribe()

    intervening = published[0]
    current = session.services["plans"].current
    assert intervening.markdown == REVISED_PLAN
    assert len({plan.token for plan in published}) == len(published)
    saved = SessionStore.load(session.store.path).custom("proposed_plan")
    assert len({(plan["plan_id"], plan["revision"]) for plan in saved}) == len(saved)
    if conditional:
        assert current == intervening
        assert session.t("plan.stale") in session.ui.text
    else:
        assert current.markdown == PLAN and current.revision == intervening.revision + 1
        session.prompt(f"/plan execute {intervening.token}")
    session.prompt(f"/plan execute {original.token}")
    assert len(calls()) == count and not session.run_pending()
    assert current.confirmed_revision is None
    assert session.services["permissions"].work_mode == "plan"

    faux.push("test", faux.reply("Execution received the selected proposal."))
    session.prompt(f"/plan execute {current.token}")
    assert len(calls()) == count + 1
    message = [message for message in session.main.messages if isinstance(message, UserMessage)][-1]
    assert current.markdown in message.text()
    assert current.markdown in str(calls()[count]["messages"][-1]["content"])


def test_reentrant_mode_exit_does_not_publish_a_plan_in_default(make_session):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    original = propose(session)
    session.prompt("/mode default")

    def leave_plan(_runtime, mode):
        if mode == "plan":
            session.prompt("/mode default")

    session.bus.on("work_mode_changed", leave_plan)
    count = len(calls())
    session.prompt(f"/plan edit {original.token}\n{REVISED_PLAN}")
    assert session.services["plans"].current == original
    assert len(session.store.custom("proposed_plan")) == 1
    assert session.services["permissions"].work_mode == "default"
    assert len(calls()) == count and not session.run_pending()


@pytest.mark.parametrize("approval,sandbox", [("auto-review", "disabled"), ("accept-all", "read-only")])
def test_execution_confirmation_and_leaving_plan_preserve_approval_and_sandbox(
    make_session, project, approval, sandbox,
):
    session = make_session(mode=approval, settings={
        "profile": "minimal", "workMode": "plan", "sandbox": {"mode": sandbox},
        "permissions": {"deny": ["write(answer.txt)"]},
    }, refresh_models=False, extensions=False)
    plan = propose(session)
    faux.push("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "blocked"})]),
              faux.reply("The existing restriction prevented the write."))
    session.prompt(f"/plan execute {plan.token}")
    assert session.services["permissions"].work_mode == "default"
    assert session.permission_mode == approval
    assert session.services["sandbox"].mode == sandbox
    assert not (project / "answer.txt").exists() and tool_results(session)[-1].is_error
    count = len(calls())
    session.prompt("/mode plan")
    session.prompt("/mode default")
    assert session.permission_mode == approval and session.services["sandbox"].mode == sandbox
    assert len(calls()) == count and not session.run_pending()


def test_saved_confirmation_is_not_replayed_or_restored_as_an_execution_grant(make_session, project):
    session = make_session(mode="auto-review", persist=True, settings={
        "profile": "minimal", "workMode": "plan", "sandbox": {"mode": "read-only"},
    }, refresh_models=False, extensions=False)
    plan = propose(session)
    faux.push("test", faux.reply("The plan was selected; no file changes were made."))
    session.prompt(f"/plan execute {plan.token}")
    assert session.services["plans"].current.confirmed_revision == plan.revision
    path = session.store.path
    session.shutdown()

    restored = make_session(mode="auto-review", session=path, settings={
        "profile": "minimal", "sandbox": {"mode": "read-only"},
    }, refresh_models=False, extensions=False)
    current = restored.services["plans"].current
    assert current.plan_id == plan.plan_id and current.revision == plan.revision
    assert current.markdown == PLAN and current.confirmed_revision is None
    assert restored.permission_mode == "auto-review" and restored.services["sandbox"].mode == "read-only"
    assert not calls() and not restored.wakeups and not restored.run_pending()
    restored.prompt("/plan show")
    assert not calls() and not (project / "answer.txt").exists()


def test_unsaved_confirmation_cannot_leave_plan_or_start_execution(make_session, project, monkeypatch):
    session = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    plan = propose(session)
    count = len(calls())
    faux.push("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "not authorized"})]),
              faux.reply("This request must not start."))
    with fail_session_write(monkeypatch, session.store.path):
        session.prompt(f"/plan execute {plan.token}")
    assert session.store.save_error is not None
    assert session.services["permissions"].work_mode == "plan"
    assert session.services["plans"].current.confirmed_revision is None
    assert len(calls()) == count and not session.run_pending()
    assert not (project / "answer.txt").exists()


@pytest.mark.parametrize("saved,configured,explicit,expected", [
    ("plan", "default", None, "plan"),
    ("default", "plan", None, "plan"),
    ("plan", "plan", "default", "default"),
    ("default", "default", "plan", "plan"),
])
def test_saved_mode_startup_floor_and_explicit_override_do_not_execute(
    make_session, saved, configured, explicit, expected,
):
    original = make_session(persist=True, settings={"profile": "minimal"},
                            refresh_models=False, extensions=False)
    original.prompt("/mode plan")
    propose(original)
    original.prompt(f"/mode {saved}")
    path = original.store.path
    original.shutdown()
    options = {"work_mode": explicit} if explicit is not None else {}
    restored = make_session(session=path, settings={"profile": "minimal", "workMode": configured},
                            refresh_models=False, extensions=False, **options)
    assert restored.services["permissions"].work_mode == expected
    assert restored.services["plans"].current.markdown == PLAN
    assert restored.services["plans"].current.confirmed_revision is None
    assert not calls() and not restored.run_pending()


def test_conversation_switches_keep_plan_and_child_permissions_with_the_origin(make_session, project):
    session = make_session(persist=True, settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
                           refresh_models=False, extensions=False)
    session.prompt("/mode plan")
    original = session.main
    original_path = session.store.path
    first = propose(session)
    api = ExtensionAPI(session, "planning-origin")
    child = api.create_runtime("planning-child", "subagent", parent=original, permission="accept-all")
    try:
        session.new_session()
        second = session.main
        assert second is not original and session.services["plans"].current is None
        assert session.services["permissions"].work_mode == "default"
        count = len(calls())
        session.prompt(f"/plan execute {first.token}")
        assert len(calls()) == count and not session.run_pending()

        faux.push("test", faux.reply(tools=[("propose_plan", {"markdown": "Unauthorized child replacement"}),
                                          ("write", {"path": "child.txt", "content": "blocked"})]),
                  faux.reply("The originating conversation is still in Plan."))
        child.agent.prompt("Attempt a write while another conversation is selected.")
        assert not (project / "child.txt").exists()
        child_results = [message for message in child.messages if isinstance(message, ToolResultMessage)]
        assert len(child_results) == 2 and all(message.is_error for message in child_results)
        assert original.proposed_plan.markdown == PLAN
        assert session.services["permissions"].mode_for("main", runtime=original) == "plan"

        faux.push("test", faux.reply(tools=[("propose_plan", {"markdown": REVISED_PLAN})]),
                  faux.reply("Only the originating proposal was revised."))
        original.agent.prompt("Revise the proposal while another conversation is selected.")
        assert session.main is second and session.services["plans"].current is None
        assert original.proposed_plan.markdown == REVISED_PLAN
        assert original.proposed_plan.revision == first.revision + 1
        session.open_session(original_path)
        assert session.services["plans"].current.markdown == REVISED_PLAN
        assert session.services["permissions"].work_mode == "plan"
        assert not session.run_pending()
    finally:
        api.release_runtime(child)


def test_busy_request_cannot_confirm_or_queue_an_execution(make_session):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    plan = propose(session)
    started, release = threading.Event(), threading.Event()
    failures = []

    def hold_request(_context):
        started.set()
        assert release.wait(5), "The test must release the active planning request"
        return faux.reply("Investigation finished; execution is still awaiting confirmation.")

    def investigate():
        try:
            session.prompt("Inspect the current plan once more.")
        except BaseException as error:
            failures.append(error)

    faux.push("test", hold_request)
    worker = threading.Thread(target=investigate)
    worker.start()
    try:
        assert started.wait(5) and session.busy
        count = len(calls())
        session.prompt(f"/plan execute {plan.token}")
        assert session.services["plans"].current.confirmed_revision is None
        assert session.services["permissions"].work_mode == "plan"
        assert len(calls()) == count and not session.wakeups
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert not session.run_pending()
