"""Built-in features: todo, sub-agents, skills, memory, tasks, jobs, schedules, teams, workflows, goals."""

from __future__ import annotations

import subprocess
import textwrap
import threading
import time
from datetime import datetime

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.features.schedule import Cron, parse_at
from virgo.features.tasks import TaskStore

from .conftest import RecordingUI, calls


def results(s):
    return [m for m in s.main.messages if isinstance(m, ToolResultMessage)]


def last_user_texts(req) -> str:
    return str(req["messages"][-1]["content"])


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# -- todo -----------------------------------------------------------------------


def test_todo_validation_and_stop_reminder(make_session):
    s = make_session(
        faux.reply(
            tools=[
                (
                    "todo_write",
                    {
                        "todos": [
                            {"content": "a", "status": "in_progress"},
                            {"content": "b", "status": "in_progress"},
                        ]
                    },
                )
            ]
        ),
        faux.reply(
            tools=[
                (
                    "todo_write",
                    {
                        "todos": [
                            {"content": "a", "status": "in_progress"},
                            {"content": "b", "status": "pending"},
                        ]
                    },
                )
            ]
        ),
        faux.reply("I'm done"),
        faux.reply("ok, finishing b"),
    )
    s.prompt("go")
    r = results(s)
    assert r[0].is_error and "only one" in r[0].text()
    assert "[~] a" in r[1].text() and "(0/2 completed)" in r[1].text()
    # stopping with open items triggers exactly one reminder
    reminders = [m for m in s.main.messages if isinstance(m, UserMessage) and m.kind == "continue"]
    assert len(reminders) == 1 and "unfinished" in reminders[0].text()
    assert s.main.agent.last_text == "ok, finishing b"


# -- sub-agents ---------------------------------------------------------------


def test_subagent_fresh_context_and_no_recursion(make_session):
    def respond(ctx):
        if "You are a Virgo sub-agent" in ctx.system:
            assert "agent" not in [t.name for t in ctx.tools]
            assert len(ctx.messages) in (1, 3)  # fresh context: just the task (+ its own tool round)
            if len(ctx.messages) == 1:
                return faux.reply(tools=[("glob", {"pattern": "*"})])
            return faux.reply("sub result: found files")
        return None

    s = make_session()
    faux.push(
        "test",
        faux.reply(tools=[("agent", {"description": "look", "prompt": "find files", "type": "explore"})]),
    )
    faux.handler("test", lambda ctx: respond(ctx) or faux.reply("main done"))
    s.prompt("delegate")
    r = results(s)
    assert r[0].text() == "sub result: found files"
    assert s.main.agent.last_text == "main done"


# -- skills -------------------------------------------------------------------------


def test_skills_listed_and_forced(make_session, project):
    d = project / ".virgo" / "skills" / "pdf"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pdf\ndescription: Work with PDF files\n---\n# PDF\nUse pypdf.")
    s = make_session(faux.reply("ok"), faux.reply("ok"))
    s.prompt("hi")
    assert "<name>pdf</name>" in calls()[0]["system"] and str(d / "SKILL.md") in calls()[0]["system"]
    s.prompt("/skill:pdf merge a.pdf b.pdf")
    assert "Use pypdf." in last_user_texts(calls()[1]) and "merge a.pdf" in last_user_texts(calls()[1])


# -- memory ---------------------------------------------------------------------------


def test_memory_save_dedupe_and_index(make_session, project, tmp_path):
    s = make_session(
        faux.reply(
            tools=[
                (
                    "memory",
                    {
                        "action": "save",
                        "name": "Prefers tabs",
                        "type": "feedback",
                        "description": "indentation",
                        "body": "Use tabs.",
                    },
                ),
                (
                    "memory",
                    {
                        "action": "save",
                        "name": "Stack",
                        "type": "project",
                        "description": "tech stack",
                        "body": "FastAPI + Postgres",
                    },
                ),
            ]
        ),
        faux.reply(
            tools=[
                (
                    "memory",
                    {
                        "action": "save",
                        "name": "Tabs again",
                        "type": "feedback",
                        "description": "x",
                        "body": "use   tabs.",
                    },
                )
            ]
        ),
        faux.reply("ok"),
    )
    s.prompt("remember")
    r = results(s)
    assert "Saved" in r[0].text() and "Saved" in r[1].text() and "Already saved" in r[2].text()
    user_dir = tmp_path / "home" / "memory"
    assert (user_dir / "prefers-tabs.md").exists() and (project / ".virgo" / "memory" / "stack.md").exists()
    assert "Prefers tabs" in (user_dir / "MEMORY.md").read_text()
    s2 = make_session(faux.reply("ok"))
    s2.prompt("hi")
    assert "Prefers tabs" in calls()[0]["system"] and "FastAPI" not in calls()[0]["system"]  # index only


# -- tasks -----------------------------------------------------------------------------


def test_task_graph(project):
    st = TaskStore(project / ".virgo" / "tasks")
    a = st.create("design")
    b = st.create("build", blocked_by=[a.id])
    c = st.create("ship", blocked_by=[b.id])
    with pytest.raises(ValueError, match="cycle"):
        st.update(a.id, "main", add_blocked_by=[c.id])
    with pytest.raises(ValueError, match="blocked"):
        st.update(b.id, "alice", status="in_progress")
    st.update(a.id, "alice", status="in_progress")
    with pytest.raises(ValueError, match="belongs to alice"):
        st.update(a.id, "bob", status="in_progress")
    _, unblocked = st.update(a.id, "alice", status="completed")
    assert [t.id for t in unblocked] == [b.id]
    claimed = st.claim_next("bob")
    assert claimed.id == b.id and claimed.owner == "bob"
    assert st.release("bob")[0].status == "pending"


def test_task_tools(make_session):
    s = make_session(
        # Tool calls in one response run in parallel; create the prerequisite first.
        faux.reply(tools=[("task_create", {"subject": "one"})]),
        faux.reply(tools=[("task_create", {"subject": "two", "blocked_by": ["1"]})]),
        faux.reply(tools=[("task_update", {"id": "1", "status": "in_progress"})]),
        faux.reply(tools=[("task_update", {"id": "1", "status": "completed"}), ("task_list", {})]),
        faux.reply("ok"),
    )
    s.prompt("plan")
    r = results(s)
    assert "#2 [blocked by 1]" in r[1].text()
    assert "@main" in r[2].text()
    assert "Now ready: #2" in r[3].text()


# -- background jobs ------------------------------------------------------------------


def test_background_job_notifies_idle_session(make_session):
    ui = RecordingUI()
    s = make_session(
        faux.reply(tools=[("bash", {"command": "sleep 0.3; echo built", "background": True})]),
        faux.reply("started"),
        ui=ui,
    )
    s.prompt("build in background")
    assert "Started background job job1" in results(s)[0].text()
    assert ui.woken.wait(5)
    faux.push("test", faux.reply("saw the result"))
    assert s.run_pending()
    note = last_user_texts(calls()[-1])
    assert "job1" in note and "completed" in note and "built" in note


# -- schedules ------------------------------------------------------------------------


def test_cron_parsing_and_matching():
    c = Cron("*/15 9-17 * * 1-5")
    assert c.matches(datetime(2026, 11, 10, 9, 30))  # Tuesday
    assert not c.matches(datetime(2026, 11, 8, 9, 30))  # Sunday
    assert not c.matches(datetime(2026, 11, 10, 9, 31))
    either = Cron("0 0 1 * 0")  # 1st of month OR Sunday
    assert either.matches(datetime(2026, 11, 1, 0, 0)) and either.matches(datetime(2026, 11, 8, 0, 0))
    assert Cron("0 12 * * 7").matches(datetime(2026, 11, 8, 12, 0))
    for bad in ["* * *", "61 * * * *", "*/0 * * * *", "5-1 * * * *"]:
        with pytest.raises(ValueError):
            Cron(bad)
    assert (parse_at("+10m", datetime(2026, 1, 1, 0, 0)) - datetime(2026, 1, 1, 0, 0)).seconds == 600


def test_schedule_fires_as_notification(make_session, project):
    ui = RecordingUI()
    s = make_session(
        faux.reply(
            tools=[("schedule", {"action": "add", "at": "+1s", "prompt": "check the build", "persist": True})]
        ),
        faux.reply("scheduled"),
        ui=ui,
    )
    s.prompt("remind me")
    assert (project / ".virgo" / "schedules.json").exists()
    sched = s.services["schedule"]
    sched.tick(datetime.now().replace(year=datetime.now().year + 1))
    assert ui.woken.is_set() and "[Scheduled s1] check the build" in str(s.wakeups[0].content)
    assert not sched.items


# -- teams ------------------------------------------------------------------------------


def test_team_messaging_plan_approval_and_shutdown(make_session):
    def respond(ctx):
        last = ctx.messages[-1]
        if "You are a Virgo teammate" not in ctx.system:
            return faux.reply("lead ack")
        if last.role == "user":
            content = "made by alice" if "approved" in str(last.content) else "too early"
            return faux.reply(tools=[("write", {"path": "out.txt", "content": content})])
        if last.tool_name == "write":
            return (
                faux.reply(tools=[("submit_plan", {"plan": "1. write out.txt"})])
                if last.is_error
                else faux.reply("implemented")
            )
        return faux.reply("waiting for approval")

    s = make_session()
    faux.push(
        "test",
        faux.reply(
            tools=[
                (
                    "team_spawn",
                    {"name": "alice", "role": "builder", "prompt": "write out.txt", "plan_first": True},
                )
            ]
        ),
    )
    faux.handler("test", respond)
    s.prompt("start a team")
    team = s.services["team"]
    assert wait_for(lambda: team.members["alice"].awaiting_plan is not None)
    rid = team.members["alice"].awaiting_plan
    # delivered while the lead was still running, or queued for its next turn
    assert wait_for(
        lambda: any(
            "requests plan approval" in str(getattr(m, "content", "")) for m in [*s.wakeups, *s.main.messages]
        )
    )
    assert not (s.cwd / "out.txt").exists()  # plan mode blocked the early write
    assert "approved" in team.review(rid, True, "go ahead")
    assert wait_for(lambda: (s.cwd / "out.txt").exists())
    assert (s.cwd / "out.txt").read_text() == "made by alice"
    assert wait_for(lambda: team.members["alice"].state == "idle")
    assert "Shutdown requested" in team.request_stop("alice")
    assert wait_for(lambda: team.members["alice"].state == "stopped")


# -- workflows --------------------------------------------------------------------------


def test_workflow_parallel_and_resume(make_session, project):
    wf = project / ".virgo" / "workflows"
    wf.mkdir(parents=True)
    (wf / "fanout.py").write_text(
        textwrap.dedent("""
        META = {"name": "fanout", "description": "score items"}
        SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
        def run(ctx, args):
            ctx.phase("scoring")
            scores = ctx.map(args["items"], lambda i: ctx.agent(f"score {i}", schema=SCHEMA, label=i)["score"])
            if args.get("fail_after"):
                raise RuntimeError("boom")
            return {"total": sum(scores)}
    """)
    )
    attempts = {"n": 0}

    def respond(ctx):
        text = str(ctx.messages[-1].content)
        if "score" in text and "workflow step" in ctx.system:
            attempts["n"] += 1
            n = int(text.split("score ")[1].split()[0])
            return faux.reply("not json" if n == 2 and attempts["n"] < 3 else f'{{"score": {n}}}')
        return faux.reply("done")

    s = make_session()
    faux.handler("test", respond)
    from virgo.features.workflow import run_workflow
    from virgo.harness.extensions import ExtensionAPI

    api = ExtensionAPI(s, "test")
    with pytest.raises(Exception, match="resume with"):
        run_workflow(api, "fanout", {"items": [1, 2, 3], "fail_after": True}, None, threading.Event())
    run_id = sorted((project / ".virgo" / "runs").glob("*.jsonl"))[0].stem
    before = len(calls())
    # resuming replays from the journal: no new model calls, still fails at the same point
    with pytest.raises(Exception, match="boom"):
        run_workflow(api, None, None, run_id, threading.Event())
    assert len(calls()) == before
    out = run_workflow(api, "fanout", {"items": [1, 2, 3]}, None, threading.Event())
    assert out["result"] == {"total": 6} and out["agents"] == 3


# -- goal -------------------------------------------------------------------------------


def test_goal_loop_continues_until_met(make_session):
    verdicts = iter(['{"met": false, "reason": "tests not run"}', '{"met": true, "reason": "tests pass"}'])

    def respond(ctx):
        if "completion evaluator" in ctx.system:
            return faux.reply(next(verdicts))
        if "Goal still active" in str(ctx.messages[-1].content):
            return faux.reply("ran tests: 3 passed")
        return faux.reply("wrote code")

    ui = RecordingUI()
    s = make_session(ui=ui)
    faux.handler("test", respond)
    s.prompt("/goal all tests pass")
    assert s.main.agent.last_text == "ran tests: 3 passed"
    assert s.services["goal"]["goal"] is None and "[goal] met" in ui.text


def test_goal_gives_up_after_cap_and_keeps_goal(make_session):
    def respond(ctx):
        if "completion evaluator" in ctx.system:
            return faux.reply('{"met": false, "reason": "nope"}')
        return faux.reply("trying")

    ui = RecordingUI()
    s = make_session(ui=ui, settings={"goal": {"maxContinues": 2}})
    faux.handler("test", respond)
    s.prompt("/goal impossible-ish")
    assert s.services["goal"]["goal"] is not None and "still not met after 2" in ui.text


def test_git_worktree_helper(project):
    from virgo.features.team import make_worktree

    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=project,
        check=True,
    )
    wt = make_worktree(project, project / ".virgo" / "worktrees" / "bob", "team/bob")
    assert (wt / ".git").exists()
