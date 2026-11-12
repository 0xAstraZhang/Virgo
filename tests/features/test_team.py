"""Teammates: messaging, plan approval, shutdown, and git worktrees."""

from __future__ import annotations

import subprocess

import pytest

from virgo.ai.providers import faux

from tests.support.harness import calls, wait_until


def test_idle_teammate_waits_for_messages_and_claims_only_explicit_tasks(make_session):
    session = make_session()
    team, tasks = session.services["team"], session.services["tasks"]
    stale = tasks.create("left over from another session")
    assigned = tasks.create("new task")

    def respond(ctx):
        if ctx.messages[-1].role == "user" and "claim assigned task" in ctx.messages[-1].text():
            return faux.reply(tools=[("task_update", {"id": assigned.id, "status": "in_progress"})])
        return faux.reply("finished delegated work")

    faux.handler("test", respond)
    member = team.spawn("alice", "helper", "one focused task", False, False)
    wait_until(lambda: member.state == "idle")
    # Reaching idle means the loop has already had its opportunity to claim stale work.
    assert tasks.get(stale.id).status == "pending" and tasks.get(stale.id).owner is None
    assert len(calls()) == 1
    tasks.update(assigned.id, "main", owner="alice")
    assert member.inbox.empty() and member.state == "idle"
    assert len(calls()) == 1
    team.send("lead", "alice", "claim assigned task")
    wait_until(lambda: tasks.get(assigned.id).status == "in_progress" and member.state == "idle")
    assert tasks.get(assigned.id).owner == "alice"
    assert len(calls()) == 3
    team.request_stop("alice")
    member.thread.join(5)
    assert not member.thread.is_alive()
    assert tasks.get(assigned.id).status == "pending" and tasks.get(assigned.id).owner is None
    assert tasks.get(stale.id).status == "pending" and tasks.get(stale.id).owner is None


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
    wait_until(lambda: team.members["alice"].awaiting_plan is not None)
    rid = team.members["alice"].awaiting_plan
    # delivered while the lead was still running, or queued for its next turn
    wait_until(
        lambda: any(
            "requests plan approval" in str(getattr(m, "content", "")) for m in [*s.wakeups, *s.main.messages]
        )
    )
    assert not (s.cwd / "out.txt").exists()  # plan mode blocked the early write
    assert "approved" in team.review(rid, True, "go ahead")
    wait_until(lambda: (s.cwd / "out.txt").exists())
    # Creation precedes writing; wait for the teammate to finish before reading.
    wait_until(lambda: team.members["alice"].state == "idle")
    assert (s.cwd / "out.txt").read_text() == "made by alice"
    assert "Shutdown requested" in team.request_stop("alice")
    wait_until(lambda: team.members["alice"].state == "stopped")


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


def test_message_arriving_as_a_teammate_turn_ends_is_delivered(make_session):
    seen = []

    def respond(ctx):
        if "You are a Virgo teammate" in ctx.system:
            seen.append(str(ctx.messages[-1].content))
        return faux.reply("ok")

    session = make_session()
    faux.handler("test", respond)
    team = session.services["team"]
    sent = []

    def late_message(rt):
        # After the loop's last steering check: only the teammate loop can deliver it.
        if rt.role == "teammate" and not sent:
            sent.append(team.send("lead", "alice", "late note"))

    session.bus.on("agent_stop", late_message, priority=100)
    team.spawn("alice", "helper", "first task", False, False)
    wait_until(lambda: any("late note" in text for text in seen))
    team.request_stop("alice")


def test_teammate_whose_runtime_cannot_start_is_stopped_and_leaves_plan_mode(make_session, monkeypatch):
    from virgo.harness import session as session_module

    session = make_session()
    team, perms = session.services["team"], session.services["permissions"]

    def broken_runtime(*args, **kwargs):
        raise RuntimeError("no runtime")

    monkeypatch.setattr(session_module, "Runtime", broken_runtime)
    with pytest.raises(RuntimeError, match="no runtime"):
        team.spawn("alice", "tester", "go", plan_first=True, worktree=False)
    assert team.members["alice"].state == "stopped"
    assert "alice" not in perms.agent_modes
    assert "alice" not in team.active_names()
