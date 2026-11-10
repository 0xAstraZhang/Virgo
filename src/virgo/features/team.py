"""Agent teams: long-lived teammates that work in parallel, message each other and share the task graph.

Each teammate is an agent on its own thread with its own conversation and session file, and an
in-process inbox. Idle teammates pick up messages first, then claim ready tasks from the task graph.
Coordination uses request/response messages with ids:
  - shutdown: ``team_stop`` -> the teammate finishes its current turn, acknowledges and exits
  - plan approval: ``plan_first`` teammates start in read-only *plan* permission mode, send a plan
    with ``submit_plan``, and get write access only after the lead approves it with ``team_review``
``worktree=true`` gives a teammate its own git worktree (branch ``team/<name>``) to avoid conflicts.
"""

from __future__ import annotations

import itertools
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import UserMessage
from ..harness.extensions import ExtensionAPI
from ..harness.store import SessionStore

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
RESERVED = {"lead", "main", "all", "*"}


@dataclass
class Teammate:
    name: str
    role: str
    cwd: Path
    plan_first: bool = False
    state: str = "starting"  # starting | working | idle | stopped
    inbox: queue.Queue = field(default_factory=queue.Queue)
    stop: bool = False
    awaiting_plan: str | None = None
    runtime: object = None
    thread: threading.Thread | None = None
    worktree: Path | None = None


class Team:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.session = api.session
        self.members: dict[str, Teammate] = {}
        self.requests: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # -- messaging -------------------------------------------------------------
    def to_lead(self, sender: str, text: str) -> None:
        self.session.notify(f"[Team] {sender}: {text}", mode="steer", source="team")

    def deliver(self, m: Teammate, item: dict) -> None:
        if m.state == "working" and item.get("type") == "message":
            m.runtime.agent.steer(
                UserMessage(f"[Message from {item['from']}]\n{item['content']}", kind="notification")
            )
        else:
            m.inbox.put(item)

    def send(self, sender: str, to: str, content: str) -> str:
        sender = "lead" if sender == "main" else sender
        if to in ("lead", "main"):
            if sender == "lead":
                return "You are the lead."
            self.to_lead(sender, content)
            return "Sent to lead."
        targets = (
            [m for m in self.members.values() if m.state != "stopped" and m.name != sender]
            if to in ("*", "all")
            else []
        )
        if not targets:
            m = self.members.get(to)
            if m is None or m.state == "stopped":
                return f"No active teammate {to!r}. Active: {', '.join(self.active_names()) or 'none'}"
            targets = [m]
        for m in targets:
            self.deliver(m, {"type": "message", "from": sender, "content": content})
        return f"Sent to {', '.join(m.name for m in targets)}."

    def active_names(self) -> list[str]:
        return [m.name for m in self.members.values() if m.state != "stopped"]

    # -- lifecycle ---------------------------------------------------------------
    def spawn(self, name: str, role: str, prompt: str, plan_first: bool, worktree: bool) -> Teammate:
        from ..harness.session import Runtime

        if not NAME_RE.match(name) or name.lower() in RESERVED:
            raise ValueError("name must be 1-32 letters/digits/-/_ and not lead/main/all")
        with self._lock:
            if name in self.members and self.members[name].state != "stopped":
                raise ValueError(f"teammate {name} already exists")
            cwd = self.session.cwd
            wt = None
            if worktree:
                wt = make_worktree(
                    self.session.cwd, self.session.paths.project / "worktrees" / name, f"team/{name}"
                )
                cwd = wt
            m = Teammate(name, role, cwd, plan_first=plan_first, worktree=wt)
            self.members[name] = m
        perms = self.session.services.get("permissions")
        if plan_first and perms:
            perms.set_agent_mode(name, "plan")
        lead_store = self.session.store
        store_path = (
            lead_store.path.parent / f"{lead_store.id}-team" / f"{name}.jsonl" if lead_store.path else None
        )
        others = ", ".join(n for n in self.active_names() if n != name) or "none yet"
        extra = (
            f"<team>\nYour name: {name}. Your role: {role}.\nThe lead coordinates the team; other teammates: {others}.\n"
            "- Report progress, questions and results with team_message(to='lead'); message teammates by name.\n"
            "- The shared task graph (task_list / task_update) holds team work; claim a task by setting it "
            "in_progress and mark it completed when done.\n"
            "- When your current work is done, report and end your turn. You are woken when messages or ready "
            "tasks arrive.\n"
            + (
                "- You start in read-only plan mode: investigate, then call submit_plan with a concrete plan and end "
                "your turn. You can modify files only after the lead approves.\n"
                if plan_first
                else ""
            )
            + (f"- You work in your own git worktree at {cwd} (branch team/{name}).\n" if wt else "")
            + "</team>"
        )
        m.runtime = Runtime(
            self.session,
            name,
            "teammate",
            cwd=cwd,
            store=SessionStore(store_path, cwd),
            system_extra=extra,
        )
        m.thread = threading.Thread(
            target=self._loop, args=(m, prompt), daemon=True, name=f"virgo-team-{name}"
        )
        m.thread.start()
        return m

    def _drain(self, m: Teammate) -> list:
        out = []
        while True:
            try:
                item = m.inbox.get_nowait()
            except queue.Empty:
                return out
            kind = item.get("type")
            if kind == "shutdown_request":
                m.stop = True
                self.to_lead(m.name, f"acknowledged shutdown request {item['request_id']} and is stopping.")
                self.requests.get(item["request_id"], {})["status"] = "done"
                return []
            if kind == "plan_response":
                m.awaiting_plan = None
                if item["approve"]:
                    perms = self.session.services.get("permissions")
                    if perms:
                        perms.set_agent_mode(m.name, None)
                    out.append(
                        UserMessage(
                            f"[Lead] Your plan was approved. {item.get('feedback') or ''}\nProceed with the implementation.",
                            kind="notification",
                        )
                    )
                else:
                    out.append(
                        UserMessage(
                            f"[Lead] Your plan was rejected: {item.get('feedback') or 'no reason given'}\nRevise it and call submit_plan again.",
                            kind="notification",
                        )
                    )
            elif kind == "message":
                out.append(
                    UserMessage(f"[Message from {item['from']}]\n{item['content']}", kind="notification")
                )

    def _loop(self, m: Teammate, prompt: str) -> None:
        pending = [UserMessage(prompt)]
        tasks = self.session.services.get("tasks")
        try:
            while not m.stop:
                pending += self._drain(m)
                if m.stop:
                    break
                if not pending and tasks is not None and not m.awaiting_plan:
                    t = tasks.claim_next(m.name)
                    if t:
                        pending = [
                            UserMessage(
                                f"[Task #{t.id} is now assigned to you] {t.subject}\n\n{t.description}\n\n"
                                "When it is done, mark it completed with task_update and report to the lead.",
                                kind="notification",
                            )
                        ]
                if not pending:
                    m.state = "idle"
                    try:
                        m.inbox.put(m.inbox.get(timeout=2.0))  # wait for mail, then drain it next iteration
                    except queue.Empty:
                        pass
                    continue
                m.state = "working"
                m.runtime.agent.prompt(pending)
                pending = []
                text = m.runtime.agent.last_text.strip()
                if text:
                    self.to_lead(m.name, f"(turn finished)\n{text[:3000]}")
        except Exception as e:  # noqa: BLE001
            self.to_lead(m.name, f"crashed: {type(e).__name__}: {e}")
        finally:
            m.state = "stopped"
            if tasks is not None:
                released = tasks.release(m.name)
                if released:
                    self.to_lead(
                        m.name, "released unfinished task(s) " + ", ".join(f"#{t.id}" for t in released)
                    )
            perms = self.session.services.get("permissions")
            if perms:
                perms.set_agent_mode(m.name, None)

    def request_stop(self, name: str, force: bool = False) -> str:
        m = self.members.get(name)
        if m is None or m.state == "stopped":
            return f"No active teammate {name!r}."
        rid = f"req{next(self._ids)}"
        self.requests[rid] = {"type": "shutdown", "to": name, "status": "pending"}
        m.inbox.put({"type": "shutdown_request", "request_id": rid})
        if force:
            m.stop = True
            m.runtime.agent.abort()
        return f"Shutdown requested ({rid}); {name} will stop after its current turn."

    def submit_plan(self, name: str, plan: str) -> str:
        m = self.members.get(name)
        if m is None:
            return "Only teammates can submit plans."
        rid = f"req{next(self._ids)}"
        self.requests[rid] = {"type": "plan", "from": name, "status": "pending", "plan": plan}
        m.awaiting_plan = rid
        self.to_lead(
            name,
            f"requests plan approval ({rid}):\n{plan}\n\nReview with team_review(request_id='{rid}', approve=..., feedback=...).",
        )
        return f"Plan submitted as {rid}. End your turn now; you will be told when the lead decides."

    def review(self, rid: str, approve: bool, feedback: str = "") -> str:
        req = self.requests.get(rid)
        if not req or req["type"] != "plan":
            return f"No plan request {rid}."
        if req["status"] != "pending":
            return f"{rid} was already {req['status']}."
        m = self.members.get(req["from"])
        if m is None or m.state == "stopped" or m.awaiting_plan != rid:
            req["status"] = "stale"
            return f"{rid} is stale (the teammate stopped or submitted a newer plan)."
        req["status"] = "approved" if approve else "rejected"
        m.inbox.put({"type": "plan_response", "request_id": rid, "approve": approve, "feedback": feedback})
        return f"Plan {rid} {req['status']}."

    def status(self) -> str:
        if not self.members:
            return "(no teammates)"
        lines = []
        for m in self.members.values():
            extra = " (awaiting plan approval)" if m.awaiting_plan else ""
            lines.append(
                f"{m.name} [{m.state}]{extra} — {m.role}"
                + (f" — worktree {m.worktree}" if m.worktree else "")
            )
        return "\n".join(lines)


def make_worktree(repo: Path, path: Path, branch: str) -> Path:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    if git("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        raise ValueError("worktrees need a git repository with at least one commit")
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    p = (
        git("worktree", "add", str(path), branch)
        if exists
        else git("worktree", "add", "-b", branch, str(path))
    )
    if p.returncode != 0:
        raise ValueError(f"git worktree add failed: {p.stderr.strip()}")
    return path


def setup(api: ExtensionAPI) -> None:
    team = Team(api)
    api.services["team"] = team

    def spawn(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            m = team.spawn(
                args["name"],
                args["role"],
                args["prompt"],
                bool(args.get("plan_first")),
                bool(args.get("worktree")),
            )
        except ValueError as e:
            return ToolResult.error(str(e))
        return ToolResult.text(
            f"Spawned teammate {m.name} ({m.role}) in {m.cwd}. It works in the background and reports with "
            "team messages; you will be notified. End your turn when you have nothing else to do."
        )

    def message(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.text(team.send(ctx.agent, args["to"], args["content"]))

    def stop(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.text(team.request_stop(args["name"], bool(args.get("force"))))

    def review(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.text(
            team.review(args["request_id"], bool(args["approve"]), args.get("feedback", ""))
        )

    def submit(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.text(team.submit_plan(ctx.agent, args["plan"]))

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
    s = {"type": "string"}
    api.register_tool(
        AgentTool(
            "team_spawn",
            "Start a long-lived teammate that works in parallel on its own thread. Give it a clear role and a "
            "complete first prompt (it cannot see this conversation). plan_first=true makes it propose a plan "
            "for your approval before changing anything; worktree=true gives it an isolated git worktree.",
            obj(
                {
                    "name": s,
                    "role": s,
                    "prompt": s,
                    "plan_first": {"type": "boolean"},
                    "worktree": {"type": "boolean"},
                },
                ["name", "role", "prompt"],
            ),
            spawn,
            snippet="start a parallel teammate agent",
            read_only=True,
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "team_message",
            "Send a message to a teammate by name, to 'lead', or to '*' (all teammates).",
            obj({"to": s, "content": s}, ["to", "content"]),
            message,
            snippet="message teammates or the lead",
            read_only=True,
        ),
        roles=("main", "teammate"),
    )
    api.register_tool(
        AgentTool(
            "team_status",
            "List teammates and their state.",
            obj({}, []),
            lambda a, c: ToolResult.text(team.status()),
            snippet="list teammates",
            read_only=True,
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "team_stop",
            "Ask a teammate to finish its current turn and shut down (force=true aborts it).",
            obj({"name": s, "force": {"type": "boolean"}}, ["name"]),
            stop,
            snippet="stop a teammate",
            read_only=True,
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "team_review",
            "Approve or reject a teammate's plan (request_id from its plan request).",
            obj({"request_id": s, "approve": {"type": "boolean"}, "feedback": s}, ["request_id", "approve"]),
            review,
            snippet="approve or reject a teammate's plan",
            read_only=True,
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "submit_plan",
            "Submit your implementation plan to the lead for approval, then end your turn.",
            obj({"plan": s}, ["plan"]),
            submit,
            snippet="submit a plan for the lead's approval",
            read_only=True,
        ),
        roles=("teammate",),
    )
    api.register_command(
        "team", lambda sess, a: api.ui.print(team.status()), "show teammates", concurrent=True
    )
    api.on(
        "pending_work", lambda sess: any(m.state in ("starting", "working") for m in team.members.values())
    )

    def shutdown(sess):
        for m in team.members.values():
            m.stop = True
            if m.runtime is not None:
                m.runtime.agent.abort()

    api.on("session_shutdown", shutdown)
