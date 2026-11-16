"""Planning workflow acceptance, specified before production changes.

Failure cases: a proposal or Todo silently changes permission Mode; an edited
plan retains an earlier confirmation; a missing, stale, or foreign token starts
execution; explicit execution loses the exact plan or broadens approvals or the
sandbox; replay starts a run or restores a grant; a conversation switch lends its
mode or plan to an older runtime/child; startup defaults discard saved Plan; a
busy model request or queued/running descendant admits execution or loses Plan.
These scenarios exercise assembled
sessions, the real tool loop, disk persistence, and actual file side effects.
"""
from __future__ import annotations

import threading

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.harness.extensions import ExtensionAPI
from virgo.harness.store import SessionSaveError, SessionStore

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


@pytest.fixture
def make_session(make_session):
    def with_plan(*args, settings=None, **kwargs):
        configured = dict(settings or {})
        configured["plugins"] = {**configured.get("plugins", {}), "plan": {"enabled": True}}
        return make_session(*args, settings=configured, **kwargs)
    return with_plan


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
    assert set(vars(plan)) == {"plan_id", "revision", "markdown"}
    assert session.main.active_plan is None
    assert session.services["permissions"].work_mode == "plan"
    assert session.approval_policy == "ask"
    assert (project / "source.txt").read_text(encoding="utf-8") == "existing source"
    assert (project / "answer.txt").read_text() == "unauthorized"
    assert not (project / "plan.md").exists()
    results = {result.tool_name: result for result in tool_results(session)}
    assert not results["read"].is_error and not results["propose_plan"].is_error
    # Prompt-only Plan does not add a hidden execution boundary. The scripted
    # model intentionally ignores its task instruction; No-Limit permits it.
    assert not results["write"].is_error and not results["todo_write"].is_error
    assert session.services["todos"]["todos"][0]["content"] == "Execute now"
    for request in calls():
        assert "propose_plan" in request["tools"]
        assert {"write", "edit", "todo_write"}.issubset(request["tools"])
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
    assert session.store.custom("plan_execution")[-1] == {"plan_id": first.plan_id, "revision": first.revision}
    assert session.main.active_plan is None
    assert session.services["permissions"].work_mode == "default"

    count = len(calls())
    session.prompt(f"/plan edit {first.token}\n{REVISED_PLAN}")
    revised = session.services["plans"].current
    assert revised.plan_id == first.plan_id and revised.revision == first.revision + 1
    assert revised.markdown == REVISED_PLAN
    assert session.main.active_plan is None
    assert session.services["permissions"].work_mode == "plan"
    session.prompt(f"/plan edit {first.token}\n{PLAN}")
    assert session.services["plans"].current == revised, "A stale editor must not overwrite the new revision"
    session.prompt(f"/plan execute {first.token}")
    session.prompt("/plan execute")
    session.prompt("/workflow default")
    assert len(calls()) == count and not session.run_pending()
    assert session.main.active_plan is None
    assert not (project / "answer.txt").exists()

    session.prompt("/workflow plan")
    faux.push("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "revised answer"})]),
              faux.reply("Implemented the confirmed revision."))
    session.prompt(f"/plan execute {revised.token}")
    assert (project / "answer.txt").read_text(encoding="utf-8") == "revised answer"
    assert not tool_results(session)[-1].is_error
    assert session.services["permissions"].work_mode == "default"
    assert session.approval_policy == "ask"
    assert session.store.custom("plan_execution")[-1] == {"plan_id": revised.plan_id, "revision": revised.revision}
    assert session.main.active_plan is None
    request = calls()[count]
    messages = [message for message in session.main.messages if isinstance(message, UserMessage)]
    assert revised.token in messages[-1].text() and REVISED_PLAN not in messages[-1].text()
    assert request["system"].count(REVISED_PLAN) == 1
    assert "write" in request["tools"] and "propose_plan" in request["tools"]


@pytest.mark.parametrize("conditional", [False, True])
def test_reentrant_mode_edit_preserves_version_bound_execution(make_session, conditional):
    session = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    original = propose(session)
    session.prompt("/workflow default")
    published, rejected = [], []
    session.bus.on("proposed_plan_changed", lambda _runtime, plan: published.append(plan))

    def revise_on_plan(runtime, mode):
        if mode == "plan":
            with pytest.raises(ValueError) as error:
                session.services["plans"].replace(REVISED_PLAN, runtime=runtime)
            rejected.append(str(error.value))

    unsubscribe = session.bus.on("work_mode_changed", revise_on_plan)
    count = len(calls())
    command = f"/plan edit {original.token}\n{PLAN}" if conditional else f"/plan edit {PLAN}"
    session.prompt(command)
    unsubscribe()

    current = session.services["plans"].current
    assert rejected and published == [current]
    assert current.markdown == PLAN and current.revision == original.revision + 1
    saved = SessionStore.load(session.store.path).custom("proposed_plan")
    assert len({(plan["plan_id"], plan["revision"]) for plan in saved}) == len(saved)
    session.prompt(f"/plan execute {original.token}")
    assert len(calls()) == count and not session.run_pending()
    assert session.main.active_plan is None
    assert session.services["permissions"].work_mode == "plan"

    faux.push("test", faux.reply("Execution received the selected proposal."))
    session.prompt(f"/plan execute {current.token}")
    assert len(calls()) == count + 1
    message = [message for message in session.main.messages if isinstance(message, UserMessage)][-1]
    assert current.token in message.text() and current.markdown not in message.text()
    assert calls()[count]["system"].count(current.markdown) == 1


def test_reentrant_mode_exit_does_not_publish_a_plan_in_default(make_session):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    original = propose(session)
    session.prompt("/workflow default")
    rejected = []

    def leave_plan(runtime, mode):
        if mode == "plan":
            with pytest.raises(ValueError) as error:
                session.services["permissions"].set_work_mode("default", runtime=runtime)
            rejected.append(str(error.value))

    session.bus.on("work_mode_changed", leave_plan)
    count = len(calls())
    session.prompt(f"/plan edit {original.token}\n{REVISED_PLAN}")
    assert rejected
    assert session.services["plans"].current.markdown == REVISED_PLAN
    assert len(session.store.custom("proposed_plan")) == 2
    assert session.services["permissions"].work_mode == "plan"
    assert len(calls()) == count and not session.run_pending()


# Preserve historical scenario IDs for cross-platform regression comparison.
@pytest.mark.parametrize("approval,sandbox", [("ask", "disabled"), ("never", "read-only")],
                         ids=["auto-review-disabled", "accept-all-read-only"])
def test_execution_confirmation_and_leaving_plan_preserve_approval_and_sandbox(
    make_session, project, approval, sandbox,
):
    session = make_session(approval=approval, settings={
        "profile": "minimal", "workMode": "plan", "sandbox": {"mode": sandbox},
        "permissions": {"deny": ["write(answer.txt)"]},
    }, refresh_models=False, extensions=False)
    plan = propose(session)
    faux.push("test", faux.reply(tools=[("write", {"path": "answer.txt", "content": "blocked"})]),
              faux.reply("The existing restriction prevented the write."))
    session.prompt(f"/plan execute {plan.token}")
    assert session.services["permissions"].work_mode == "default"
    assert session.approval_policy == approval
    assert session.services["sandbox"].mode == sandbox
    assert not (project / "answer.txt").exists() and tool_results(session)[-1].is_error
    count = len(calls())
    session.prompt("/workflow plan")
    session.prompt("/workflow default")
    assert session.approval_policy == approval and session.services["sandbox"].mode == sandbox
    assert len(calls()) == count and not session.run_pending()


def test_saved_confirmation_is_not_replayed_or_restored_as_an_execution_grant(make_session, project):
    session = make_session(approval="ask", allow=False, persist=True, settings={
        "profile": "minimal", "workMode": "plan", "sandbox": {"mode": "read-only"},
    }, refresh_models=False, extensions=False)
    plan = propose(session)
    faux.push("test", faux.reply("The plan was selected; no file changes were made."))
    session.prompt(f"/plan execute {plan.token}")
    assert session.store.custom("plan_execution")[-1] == {"plan_id": plan.plan_id, "revision": plan.revision}
    assert session.main.active_plan is None
    path = session.store.path
    session.shutdown()

    restored = make_session(approval="ask", allow=False, session=path, settings={
        "profile": "minimal", "sandbox": {"mode": "read-only"},
    }, refresh_models=False, extensions=False)
    current = restored.services["plans"].current
    assert current.plan_id == plan.plan_id and current.revision == plan.revision
    assert current.markdown == PLAN
    assert restored.main.active_plan is None
    assert restored.approval_policy == "ask" and restored.services["sandbox"].mode == "read-only"
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
    assert session.main.active_plan is None
    assert len(calls()) == count and not session.run_pending()
    assert not (project / "answer.txt").exists()

    # A notification can encounter a later disk error after execution intent
    # was persisted. Admission still must not retain a plan for a run that never starts.
    notified = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan"},
                            refresh_models=False, extensions=False)
    selected = propose(notified)
    count, previous_run = len(calls()), notified.main.run_id
    unsubscribe = notified.bus.on("work_mode_changed", lambda runtime, _mode: runtime.store.append(
        "custom", kind="mode_observation", data={},
    ))
    with fail_session_write(monkeypatch, notified.store.path, containing="mode_observation"):
        notified.prompt(f"/plan execute {selected.token}")
    unsubscribe()
    assert notified.store.save_error is not None
    assert notified.services["permissions"].work_mode == "default"
    assert notified.main.active_plan is None
    assert len(calls()) == count and notified.main.run_id == previous_run
    assert not notified.busy and not notified.run_pending()


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
    original.prompt("/workflow plan")
    propose(original)
    original.prompt(f"/workflow {saved}")
    path = original.store.path
    original.shutdown()
    options = {"work_mode": explicit} if explicit is not None else {}
    restored = make_session(session=path, settings={"profile": "minimal", "workMode": configured},
                            refresh_models=False, extensions=False, **options)
    assert restored.services["permissions"].work_mode == expected
    assert restored.services["plans"].current.markdown == PLAN
    assert restored.main.active_plan is None
    assert not calls() and not restored.run_pending()


def test_invalid_saved_proposal_cannot_silently_become_an_empty_plan(make_session):
    original = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan"},
                            refresh_models=False, extensions=False)
    original.store.append("custom", kind="proposed_plan", data={"plan_id": "invalid"})
    path = original.store.path
    original.shutdown()
    restored = make_session(session=path, settings={"profile": "minimal"},
                            refresh_models=False, extensions=False)
    faux.push("test", faux.reply("A saved proposal must not disappear from my context."))
    restored.prompt("Continue the saved planning task.")
    assert not calls()
    assert "Invalid saved ProposedPlan" in restored.ui.text
    with pytest.raises(ValueError, match="Invalid saved ProposedPlan"):
        restored.services["plans"].replace(PLAN)
    assert SessionStore.load(path).custom("proposed_plan") == [{"plan_id": "invalid"}]


@pytest.mark.parametrize("retained", [False, True])
def test_failed_startup_override_keeps_the_original_plan_ceiling(make_session, monkeypatch, retained):
    session = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    propose(session)
    origin, path = session.main, session.store.path
    if retained:
        session.new_session()
    selected = session.main
    revision = origin.work_mode_revision

    with fail_session_write(monkeypatch, path), pytest.raises(SessionSaveError):
        session.open_session(path, work_mode="default")

    assert session.main is selected
    assert origin.work_mode == "plan"
    assert origin.work_mode_revision == revision
    assert session.services["permissions"].work_mode_for(origin) == "plan"
    assert not session.run_pending()


def test_conversation_switches_keep_plan_and_child_permissions_with_the_origin(make_session, project):
    session = make_session(persist=True, settings={"profile": "minimal", "sandbox": {"mode": "workspace-write"}},
                           refresh_models=False, extensions=False)
    session.prompt("/workflow plan")
    original = session.main
    original_path = session.store.path
    first = propose(session)
    api = ExtensionAPI(session, "planning-origin")
    child = api.create_runtime("planning-child", "subagent", parent=original, read_only=False)
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
        assert (project / "child.txt").read_text() == "blocked"
        child_results = [message for message in child.messages if isinstance(message, ToolResultMessage)]
        assert len(child_results) == 2 and child_results[0].is_error and not child_results[1].is_error
        assert original.proposed_plan.markdown == PLAN
        assert session.services["permissions"].work_mode_for(original) == "plan"

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
        assert session.main.active_plan is None
        assert session.services["permissions"].work_mode == "plan"
        assert len(calls()) == count and not session.wakeups
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert not session.run_pending()


@pytest.mark.parametrize("permission_mode,stage,action", [
    pytest.param("no-limit", "queued", "mode", id="no-limit-queued-mode"),
    pytest.param("no-limit", "queued", "execute", id="no-limit-queued-execute"),
    pytest.param("no-limit", "running", "mode", id="no-limit-running-mode"),
    pytest.param("no-limit", "running", "execute", id="no-limit-running-execute"),
    pytest.param("read-only", "queued", "mode", id="queued-mode"),
    pytest.param("read-only", "queued", "execute", id="queued-execute"),
    pytest.param("read-only", "running", "mode", id="running-mode"),
    pytest.param("read-only", "running", "execute", id="running-execute"),
])
def test_background_planning_child_blocks_only_its_origin_before_confirmation(
    make_session, project, monkeypatch, action, stage, permission_mode,
):
    session = make_session(persist=True, settings={
        "profile": "minimal", "workMode": "plan",
        "plugins": {"subagent": {"enabled": True}, "background": {"enabled": True}},
    }, refresh_models=False, extensions=False)
    session.services["sandbox"].select_mode(permission_mode)
    plan = propose(session)
    origin, origin_path = session.main, session.store.path
    entered, release = threading.Event(), threading.Event()
    child_results = []

    def record_child(event, runtime):
        if (runtime.role != "main" and event.type == "message_end"
                and isinstance(event.message, ToolResultMessage)):
            child_results.append(event.message)

    session.bus.on("agent_event", record_child)
    subagents = session.services["subagents"]
    run_child = subagents.run

    def delayed_child(*args, **kwargs):
        if stage == "queued":
            entered.set()
            assert release.wait(5), "Release the job before its child Runtime is created"
        return run_child(*args, **kwargs)

    monkeypatch.setattr(subagents, "run", delayed_child)

    def respond(context):
        if context.messages[0].text() == "Investigate the source in Plan":
            if isinstance(context.messages[-1], ToolResultMessage):
                return faux.reply("The investigation is complete.")
            if stage == "running":
                entered.set()
                assert release.wait(5), "Release the child's in-flight Plan request"
            return faux.reply(tools=[("write", {"path": "child.txt", "content": "unauthorized"})])
        if context.messages[-1].text() == "Start a background investigation":
            return faux.reply(tools=[("agent", {
                "description": "Investigate the source", "prompt": "Investigate the source in Plan",
                "background": True,
            })])
        return faux.reply("The lead is idle.")

    faux.handler("test", respond)
    job = None
    try:
        session.prompt("Start a background investigation")
        assert entered.wait(5)
        job = next(iter(session.services["jobs"].jobs.values()))
        assert not session.busy and not origin.agent.running
        assert bool(session._child_runtimes) == (stage == "running")

        # Work owned by another conversation must not prevent this one executing.
        session.new_session()
        session.prompt("/plan edit # A separate conversation's proposal")
        other = session.services["plans"].current
        session.prompt(f"/plan execute {other.token}")
        assert session.store.custom("plan_execution")[-1] == {"plan_id": other.plan_id, "revision": other.revision}
        assert session.main.active_plan is None
        assert session.services["permissions"].work_mode == "default"
        session.open_session(origin_path)
        # A queued child owns its submission-time profile even if its parent
        # selects wider permissions and starts another run before child creation.
        session.services["sandbox"].select_mode("no-limit")
        session.prompt("Continue the lead's work with the newly selected permissions")

        count = len(calls())
        saved = session.store.custom("proposed_plan")
        session.prompt("/workflow default" if action == "mode" else f"/plan execute {plan.token}")
        selected_mode = session.services["permissions"].work_mode
        selected_plan = session.services["plans"].current
        saved_after = SessionStore.load(origin_path).custom("proposed_plan")
        calls_after = len(calls())
    finally:
        release.set()
        if job is not None:
            assert job.done.wait(5)

    assert child_results
    if permission_mode == "read-only":
        assert not (project / "child.txt").exists()
        assert child_results[0].is_error
        assert child_results[0].details["sandbox"]["dimension"] == "read_only"
    else:
        assert (project / "child.txt").read_text() == "unauthorized"
        assert not child_results[0].is_error
    assert selected_mode == "plan" and selected_plan is plan
    assert saved_after == saved, "Rejected execution must not persist a confirmation"
    assert calls_after == count
    assert session.t("plan.busy" if action == "execute" else "work_mode.busy") in session.ui.text

    # Completion releases admission without removing the existing job record.
    session.prompt(f"/plan execute {plan.token}")
    assert session.store.custom("plan_execution")[-1] == {"plan_id": plan.plan_id, "revision": plan.revision}
    assert session.main.active_plan is None
    assert session.services["permissions"].work_mode == "default"


def test_reentrant_pending_work_inspection_cannot_execute_a_revised_plan(make_session):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    original = propose(session)
    inspected = []

    def inspect(runtime):
        inspected.append(runtime)
        session.services["plans"].replace(REVISED_PLAN, runtime=runtime)
        return False

    unsubscribe = session.bus.on("runtime_pending_work", inspect)
    count = len(calls())
    faux.push("test", faux.reply("The selected plan is stable throughout admission."))
    session.prompt(f"/plan execute {original.token}")
    unsubscribe()

    assert not inspected, "Core admission does not dispatch mutable extension inspection callbacks"
    assert session.services["plans"].current is original
    assert session.main.active_plan is None
    assert session.services["permissions"].work_mode == "default"
    assert len(calls()) == count + 1
    assert calls()[count]["system"].count(PLAN) == 1


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("change", ["revise", "mode", "switch", "new_default", "review"])
def test_early_admission_observers_preserve_newer_selection(make_session, project, phase, change):
    session = make_session(persist=True, settings={"profile": "minimal", "workMode": "plan",
                                                  "plugins": {"code_review": {"enabled": True}}},
                           refresh_models=False, extensions=False)
    original = propose(session)
    origin, origin_path = session.main, session.store.path
    permissions = session.services["permissions"]
    rejected = []

    def observe(runtime, _value):
        session.prompt("/plan show")  # Read-only presentation remains available during notifications.
        if change == "review":
            try:
                session.prompt("/review")
            except ValueError as error:
                rejected.append(str(error))
            return
        with pytest.raises(ValueError) as error:
            if change == "revise":
                session.services["plans"].replace("# Reentrant replacement", runtime=runtime)
            elif change == "mode":
                permissions.set_work_mode("default", runtime=runtime)
            elif change == "new_default":
                session.new_session(work_mode="default")
            else:
                session.open_session(origin_path)
        rejected.append(str(error.value))

    event = "work_mode_changed" if phase == 1 else "proposed_plan_changed"
    unsubscribe = session.bus.on(event, observe)
    count = len(calls())
    faux.push("test", faux.reply("Execute only the version selected outside the notification."))
    session.prompt(f"/plan execute {original.token}" if phase == 1 else f"/plan edit {REVISED_PLAN}")
    unsubscribe()

    assert not origin.store.custom("code_review"), "Rejected tasks cannot publish a review lifecycle"
    assert rejected and session.main is origin
    assert len(calls()) == count + (phase == 1)
    assert origin.proposed_plan.markdown == (PLAN if phase == 1 else REVISED_PLAN)
    assert origin.active_plan is None
    assert permissions.work_mode_for(origin) == ("default" if phase == 1 else "plan")
    # The committed selection controls later work; notification attempts cannot change it.
    faux.script("test", faux.reply(tools=[("write", {"path": "observer.txt", "content": "answer"})]),
                faux.reply("The committed mode controls this operation."))
    session.prompt("Report the current investigation status.")
    assert (project / "observer.txt").read_text() == "answer"
    assert not tool_results(session)[-1].is_error


def test_admission_inspection_rechecks_a_child_started_by_its_observer(make_session, project):
    session = make_session(settings={"profile": "minimal", "workMode": "plan"},
                           refresh_models=False, extensions=False)
    api = ExtensionAPI(session, "notification-child")
    rejected = []

    def observe(runtime, _mode):
        with pytest.raises(ValueError) as error:
            api.create_runtime("new-investigator", "subagent", parent=runtime)
        rejected.append(str(error.value))

    unsubscribe = session.bus.on("work_mode_changed", observe)
    session.prompt("/workflow default")
    unsubscribe()
    assert rejected and not session._child_runtimes
    assert session.services["permissions"].work_mode == "default"
    assert not calls() and not (project / "observer-child.txt").exists()


@pytest.mark.parametrize("action", ["mode", "execute"])
def test_admission_observer_cannot_queue_untracked_plan_work(make_session, project, action):
    session = make_session(settings={
        "profile": "minimal", "workMode": "plan",
        "plugins": {"subagent": {"enabled": True}, "background": {"enabled": True}},
    }, refresh_models=False, extensions=False)
    plan = propose(session)
    jobs = session.services["jobs"]
    rejected = []

    def observe(runtime, _mode):
        with pytest.raises(ValueError) as error:
            jobs.start_callable("Unrequested investigation", lambda _signal: "must not run",
                                store=runtime.origin.root, agent=runtime.name, cwd=runtime.cwd)
        rejected.append(str(error.value))

    unsubscribe = session.bus.on("work_mode_changed", observe)
    count = len(calls())
    faux.push("test", faux.reply("The committed selection controls this request."))
    session.prompt("/workflow default" if action == "mode" else f"/plan execute {plan.token}")
    unsubscribe()

    assert rejected and not jobs.jobs and not session._child_runtimes
    assert not session._pending_child_work and not (project / "queued-observer.txt").exists()
    assert session.services["permissions"].work_mode == "default"
    assert len(calls()) == count + (action == "execute")
    assert session.main.active_plan is None


def test_legacy_migration_preserves_a_trashed_origin_with_queued_child_work(make_session, project, trash):
    session = make_session(persist=True, settings={
        "profile": "minimal", "workMode": "plan",
        "plugins": {"subagent": {"enabled": True}, "background": {"enabled": True}},
    }, refresh_models=False, extensions=False)
    origin, origin_path = session.main, session.store.path
    entered, release = threading.Event(), threading.Event()

    def investigate(signal):
        entered.set()
        assert release.wait(5), "Release the queued investigation after its origin was removed"
        return session.services["subagents"].run("Investigate only", "Queued investigation", signal,
                                                project, origin.origin)

    job = session.services["jobs"].start_callable("Queued investigation", investigate,
                                               store=origin.origin.root)
    try:
        assert entered.wait(5) and not session._child_runtimes
        session.trash_session(origin_path)
        trashed = trash[0][1]
        saved_trash = trashed.read_bytes()
        session.prompt("/workflow default")
        assert session.services["permissions"].work_mode == "default"
        faux.push("test", faux.reply(tools=[("write", {"path": "trashed-child.txt", "content": "blocked"})]),
                  faux.reply("The originating Plan boundary was retained."))
    finally:
        release.set()
        assert job.done.wait(5)

    assert (project / "trashed-child.txt").read_text() == "blocked"
    assert session.services["permissions"].work_mode_for(origin) == "plan"
    assert origin.store.path is None and not origin_path.exists()
    assert trashed.read_bytes() == saved_trash
    assert not session.runtime_is_busy(origin)
