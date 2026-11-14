"""Agent teams: long-lived teammates that work in parallel, message each other and share the task graph.

Each teammate is an agent on its own thread with its own conversation and session file, and an
in-process inbox. Idle teammates wait for messages; tasks are claimed explicitly with task_update.
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
from ..ai.types import AssistantMessage, UserMessage
from ..diagnostics import error_summary
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
    accounting_store: object = field(default=None, repr=False)
    error_details: dict | None = None
    related_errors: list[dict] = field(default_factory=list)


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
        member = self.members.get(sender)
        owner = member.accounting_store if member is not None else None
        if member is not None:
            evidence = (
                [member.error_details] if member.error_details is not None else []
            ) + member.related_errors
            self.session.retain_diagnostics(owner, evidence)
        try:
            self.session.notify(
                self.session.t("features.team.notification", sender=sender, text=text),
                mode="steer",
                source="team",
                store=owner,
            )
        except Exception as error:
            if member is not None:
                self._report(member, error, phase="team.notify")
            else:
                self.session.report_error(error, phase="team.notify", context={"agent": sender})
            raise

    def _report(self, member: Teammate, error, *, phase: str) -> dict:
        context = {"agent": member.name, "role": "teammate"}
        if member.error_details is not None:
            context["related_error_id"] = member.error_details["error_id"]
        if member.runtime is not None:
            details = member.runtime.report_error(error, phase=phase, context=context)
        else:
            details = self.session.report_error(
                error, phase=phase, context=context, store=member.accounting_store
            )
        if member.error_details is None:
            member.error_details = details
        elif details["error_id"] != member.error_details["error_id"] and all(
            item["error_id"] != details["error_id"] for item in member.related_errors
        ):
            member.related_errors.append(details)
        return details

    def _notify(self, member: Teammate, text: str) -> None:
        """A failed delivery must not interrupt lifecycle cleanup or lose its cause."""
        try:
            self.to_lead(member.name, text)
        except Exception as error:
            self._report(member, error, phase="team.notify")

    def deliver(self, m: Teammate, item: dict) -> None:
        if m.state == "working" and item.get("type") == "message":
            m.runtime.agent.steer(
                UserMessage(
                    self.session.t("features.team.message", sender=item["from"], content=item["content"]),
                    kind="notification",
                )
            )
        else:
            m.inbox.put(item)

    def send(self, sender: str, to: str, content: str) -> str:
        sender = "lead" if sender == "main" else sender
        if to in ("lead", "main"):
            if sender == "lead":
                return self.session.t("features.team.you_lead")
            self.to_lead(sender, content)
            return self.session.t("features.team.sent_lead")
        targets = (
            [m for m in self.members.values() if m.state != "stopped" and m.name != sender]
            if to in ("*", "all")
            else []
        )
        if not targets:
            m = self.members.get(to)
            if m is None or m.state == "stopped":
                return self.session.t(
                    "features.team.unknown_active",
                    name=to,
                    active=", ".join(self.active_names()) or self.session.t("features.team.none"),
                )
            targets = [m]
        for m in targets:
            self.deliver(m, {"type": "message", "from": sender, "content": content})
        return self.session.t("features.team.sent", recipients=", ".join(m.name for m in targets))

    def active_names(self) -> list[str]:
        return [m.name for m in self.members.values() if m.state != "stopped"]

    # -- lifecycle ---------------------------------------------------------------
    def spawn(
        self, name: str, role: str, prompt: str, plan_first: bool, worktree: bool, review_parent=None
    ) -> Teammate:
        if review_parent is None:
            with self.session._lock:
                review_parent = self.session.main.review_source
        review_parent.root.entries()
        if not NAME_RE.match(name) or name.lower() in RESERVED:
            raise ValueError(self.session.t("features.team.invalid_name"))
        with self._lock:
            if name in self.members and self.members[name].state != "stopped":
                raise ValueError(self.session.t("features.team.exists", name=name))
            cwd = self.session.cwd
            wt = None
            if worktree:
                wt = make_worktree(
                    self.session.cwd,
                    self.session.paths.project / "worktrees" / name,
                    f"team/{name}",
                    self.session.t,
                )
                cwd = wt
            m = Teammate(
                name, role, cwd, plan_first=plan_first, worktree=wt, accounting_store=review_parent.root
            )
            self.members[name] = m
        perms = self.session.services.get("permissions")
        lead_store = review_parent.store if review_parent is not None else self.session.store
        store_path = (
            lead_store.path.parent / f"{lead_store.id}-team" / f"{name}.jsonl" if lead_store.path else None
        )
        others = ", ".join(n for n in self.active_names() if n != name) or "none yet"
        extra = (
            f"<team>\nYour name: {name}. Your role: {role}.\nThe lead coordinates the team; other teammates: {others}.\n"
            "- team_message(to='lead') reaches the lead; teammate names address their inboxes.\n"
            "- task_list / task_update expose the shared task graph. Set status=in_progress to claim a task.\n"
            "- After a turn ends, you wait for messages. Task ownership changes do not wake you.\n"
            + (
                "- You start in read-only plan mode: investigate, then call submit_plan with a concrete plan and end "
                "your turn. You can modify files only after the lead approves.\n"
                if plan_first
                else ""
            )
            + (f"- You work in your own git worktree at {cwd} (branch team/{name}).\n" if wt else "")
            + "</team>"
        )
        try:
            if plan_first and perms:
                perms.set_agent_mode(name, "plan")
            m.runtime = self.session.create_runtime(
                name,
                "teammate",
                cwd=cwd,
                store=SessionStore(store_path, cwd),
                system_extra=extra,
                review_parent=review_parent,
            )
            m.thread = threading.Thread(
                target=self._loop, args=(m, prompt), daemon=True, name=f"virgo-team-{name}"
            )
            m.thread.start()
        except BaseException as error:
            # A teammate that never started must not look busy or keep plan mode.
            m.state = "stopped"
            if isinstance(error, Exception):
                self._report(m, error, phase="team.spawn")
            if plan_first and perms:
                try:
                    perms.set_agent_mode(name, None)
                except Exception as cleanup_error:
                    self._report(m, cleanup_error, phase="team.cleanup")
            raise
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
                self._notify(m, self.session.t("features.team.stopping", id=item["request_id"]))
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
                            self.session.t(
                                "features.team.approved_notice", feedback=item.get("feedback") or ""
                            ),
                            kind="notification",
                        )
                    )
                else:
                    out.append(
                        UserMessage(
                            self.session.t(
                                "features.team.rejected_notice",
                                feedback=item.get("feedback") or self.session.t("features.team.no_reason"),
                            ),
                            kind="notification",
                        )
                    )
            elif kind == "message":
                out.append(
                    UserMessage(
                        self.session.t("features.team.message", sender=item["from"], content=item["content"]),
                        kind="notification",
                    )
                )

    def _loop(self, m: Teammate, prompt: str) -> None:
        pending = [UserMessage(prompt)]
        tasks = self.session.services.get("tasks")
        try:
            while not m.stop:
                if not m.runtime.can_run():
                    break
                pending += self._drain(m)
                if m.stop:
                    break
                if not pending:
                    m.state = "idle"
                    try:
                        m.inbox.put(m.inbox.get(timeout=2.0))  # wait for mail, then drain it next iteration
                    except queue.Empty:
                        pass
                    continue
                m.state = "working"
                m.runtime.agent.prompt(pending)
                # Messages steered in after the agent's last queue check would
                # otherwise wait for an unrelated future turn.
                pending = m.runtime.agent.drain_queues()
                last = next(
                    (
                        message
                        for message in reversed(m.runtime.messages)
                        if isinstance(message, AssistantMessage)
                    ),
                    None,
                )
                if last is not None and last.stop_reason == "error":
                    details = self._report(
                        m,
                        last.error_details or last.error_message or "Teammate request failed.",
                        phase="team.model",
                    )
                    self._notify(
                        m,
                        self.session.t(
                            "features.team.turn_failed", error=error_summary(details, t=self.session.t)
                        ),
                    )
                else:
                    text = m.runtime.agent.last_text.strip()
                    if text:
                        self._notify(m, self.session.t("features.team.turn_finished", text=text[:3000]))
        except Exception as e:  # noqa: BLE001
            details = self._report(m, e, phase="team.loop")
            self._notify(
                m, self.session.t("features.team.crashed", error=error_summary(details, t=self.session.t))
            )
        finally:
            m.state = "stopped"
            if tasks is not None:
                try:
                    released = tasks.release(m.name)
                    if released:
                        self._notify(
                            m,
                            self.session.t("features.team.released")
                            + ", ".join(f"#{t.id}" for t in released),
                        )
                except Exception as error:
                    self._report(m, error, phase="team.cleanup")
            perms = self.session.services.get("permissions")
            if perms:
                try:
                    perms.set_agent_mode(m.name, None)
                except Exception as error:
                    self._report(m, error, phase="team.cleanup")

    def request_stop(self, name: str, force: bool = False) -> str:
        m = self.members.get(name)
        if m is None or m.state == "stopped":
            return self.session.t("features.team.unknown", name=name)
        rid = f"req{next(self._ids)}"
        self.requests[rid] = {"type": "shutdown", "to": name, "status": "pending"}
        m.inbox.put({"type": "shutdown_request", "request_id": rid})
        if force:
            m.stop = True
            if m.runtime is not None:
                m.runtime.agent.abort()
        return self.session.t("features.team.stop_requested", id=rid, name=name)

    def submit_plan(self, name: str, plan: str) -> str:
        m = self.members.get(name)
        if m is None:
            return self.session.t("features.team.only_teammates")
        rid = f"req{next(self._ids)}"
        self.requests[rid] = {"type": "plan", "from": name, "status": "pending", "plan": plan}
        m.awaiting_plan = rid
        self.to_lead(
            name,
            self.session.t("features.team.plan_request", id=rid, plan=plan),
        )
        return self.session.t("features.team.plan_submitted", id=rid)

    def review(self, rid: str, approve: bool, feedback: str = "") -> str:
        req = self.requests.get(rid)
        if not req or req["type"] != "plan":
            return self.session.t("features.team.plan_missing", id=rid)
        if req["status"] != "pending":
            return self.session.t(
                "features.team.plan_already",
                id=rid,
                status=self.session.t("features.team.state." + req["status"]),
            )
        m = self.members.get(req["from"])
        if m is None or m.state == "stopped" or m.awaiting_plan != rid:
            req["status"] = "stale"
            return self.session.t("features.team.plan_stale", id=rid)
        req["status"] = "approved" if approve else "rejected"
        m.inbox.put({"type": "plan_response", "request_id": rid, "approve": approve, "feedback": feedback})
        return self.session.t(
            "features.team.plan_result", id=rid, status=self.session.t("features.team.state." + req["status"])
        )

    def status(self) -> str:
        if not self.members:
            return self.session.t("features.team.empty")
        lines = []
        for m in self.members.values():
            extra = self.session.t("features.team.awaiting") if m.awaiting_plan else ""
            lines.append(
                f"{m.name} [{self.session.t('features.team.state.' + m.state)}]{extra} — {m.role}"
                + (self.session.t("features.team.worktree", path=m.worktree) if m.worktree else "")
                + (
                    self.session.t("features.team.error", id=m.error_details["error_id"])
                    if m.error_details
                    else ""
                )
            )
        return "\n".join(lines)

    def errors(self) -> list[dict]:
        return [
            error
            for member in self.members.values()
            for error in ([member.error_details] if member.error_details else []) + member.related_errors
        ]


def make_worktree(repo: Path, path: Path, branch: str, t=None) -> Path:
    from ..i18n import Translator

    t = t or Translator()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    if git("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        raise ValueError(t("features.team.worktree_git"))
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
        raise ValueError(t("features.team.worktree_failed", error=p.stderr.strip()))
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
                review_parent=ctx.review_source,
            )
        except ValueError as e:
            details = getattr(e, "_virgo_error_details", None)
            return ToolResult.error(str(e), details={"error": details} if details is not None else None)
        return ToolResult.text(ctx.t("features.team.spawned", name=m.name, role=m.role, cwd=m.cwd))

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
            "Start a long-lived teammate with an independent context; include its task context in prompt. "
            "It waits for messages after finishing a turn. plan_first=true requires a plan "
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
            read_only=True,
            i18n_key="tools.team_spawn",
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "team_message",
            "Send a message to a teammate by name, to 'lead', or to '*' (all teammates). "
            "Wakes an idle recipient; reaches a working recipient at a message boundary.",
            obj({"to": s, "content": s}, ["to", "content"]),
            message,
            read_only=True,
            i18n_key="tools.team_message",
        ),
        roles=("main", "teammate"),
    )
    api.register_tool(
        AgentTool(
            "team_status",
            "List teammates and their state.",
            obj({}, []),
            lambda a, c: ToolResult.text(team.status(), details={"related_errors": team.errors()}),
            read_only=True,
            i18n_key="tools.team_status",
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "team_stop",
            "Ask a teammate to finish its current turn and shut down (force=true aborts it).",
            obj({"name": s, "force": {"type": "boolean"}}, ["name"]),
            stop,
            read_only=True,
            i18n_key="tools.team_stop",
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "team_review",
            "Approve or reject a teammate's plan (request_id from its plan request).",
            obj({"request_id": s, "approve": {"type": "boolean"}, "feedback": s}, ["request_id", "approve"]),
            review,
            read_only=True,
            i18n_key="tools.team_review",
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "submit_plan",
            "Submit your implementation plan to the lead for approval, then end your turn.",
            obj({"plan": s}, ["plan"]),
            submit,
            read_only=True,
            i18n_key="tools.submit_plan",
        ),
        roles=("teammate",),
    )
    api.register_command(
        "team",
        lambda sess, a: api.ui.print(team.status()),
        "show teammates",
        concurrent=True,
        description_key="features.commands.team",
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
