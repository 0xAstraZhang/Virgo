"""Task graph: durable, dependency-aware work items shared by the lead and teammates.

Stored as ``.virgo/tasks/<id>.json``; all mutations happen under a file lock so several agents
(threads or processes) can claim work safely. Claiming = setting status ``in_progress``; it is
refused while dependencies are unfinished or another agent owns the task.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..harness.extensions import ExtensionAPI

STATUSES = ("pending", "in_progress", "completed")

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


class TaskStore:
    _thread_lock = threading.RLock()

    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def lock(self):
        with self._thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / ".lock", "w") as fh:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl:
                        fcntl.flock(fh, fcntl.LOCK_UN)

    def _path(self, tid: str) -> Path:
        # Ids come from tool arguments: never let one name a file outside the task directory.
        if not tid or Path(tid).name != tid or tid.startswith("."):
            raise ValueError(f"invalid task id {tid!r}")
        return self.root / f"{tid}.json"

    def get(self, tid: str) -> Task | None:
        try:
            return Task(**json.loads(self._path(str(tid)).read_text(encoding="utf-8")))
        except (ValueError, OSError, TypeError):  # includes invalid ids and JSONDecodeError
            return None

    def all(self) -> list[Task]:
        if not self.root.is_dir():
            return []
        tasks = [t for f in self.root.glob("*.json") if (t := self.get(f.stem))]
        return sorted(tasks, key=lambda t: int(t.id) if t.id.isdigit() else 0)

    def _save(self, t: Task) -> None:
        t.updated = time.time()
        tmp = self._path(t.id).with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(asdict(t), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path(t.id))

    # -- queries -------------------------------------------------------------
    def blockers(self, t: Task) -> list[str]:
        out = []
        for d in t.blocked_by:
            dep = self.get(d)
            if dep is None or dep.status != "completed":
                out.append(d)
        return out

    def ready(self, t: Task) -> bool:
        return t.status == "pending" and not self.blockers(t)

    def _reaches(self, start: str, target: str) -> bool:
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            t = self.get(cur)
            if t:
                stack.extend(t.blocked_by)
        return False

    # -- mutations -----------------------------------------------------------
    def create(self, subject: str, description: str = "", blocked_by: list[str] | None = None) -> Task:
        with self.lock():
            deps = [str(d) for d in blocked_by or []]
            for d in deps:
                if self.get(d) is None:
                    raise ValueError(f"unknown task {d}")
            nums = [int(f.stem) for f in self.root.glob("*.json") if f.stem.isdigit()]
            t = Task(str(max(nums, default=0) + 1), subject.strip(), description.strip(), blocked_by=deps)
            self._save(t)
            return t

    def update(self, tid: str, actor: str, **changes) -> tuple[Task, list[Task]]:
        """Apply changes; returns (task, newly_unblocked)."""
        with self.lock():
            t = self.get(tid)
            if t is None:
                raise ValueError(f"unknown task {tid}")
            for d in changes.get("add_blocked_by") or []:
                d = str(d)
                if d == t.id:
                    raise ValueError("a task cannot depend on itself")
                if self.get(d) is None:
                    raise ValueError(f"unknown task {d}")
                if self._reaches(d, t.id):
                    raise ValueError(f"adding {d} would create a dependency cycle")
                if d not in t.blocked_by:
                    t.blocked_by.append(d)
            for d in changes.get("remove_blocked_by") or []:
                t.blocked_by = [x for x in t.blocked_by if x != str(d)]
            for k in ("subject", "description"):
                if changes.get(k):
                    setattr(t, k, changes[k])
            status, owner = changes.get("status"), changes.get("owner")
            if status and status not in STATUSES:
                raise ValueError(f"status must be one of {STATUSES}")
            ready_before = {
                x.id for x in self.all() if x.status == "pending" and x.blocked_by and not self.blockers(x)
            }
            if status == "in_progress":
                if self.blockers(t):
                    raise ValueError(
                        f"task {t.id} is blocked by unfinished task(s) {', '.join(self.blockers(t))}"
                    )
                new_owner = owner or actor
                if t.owner and t.owner != new_owner and actor != "main":
                    raise ValueError(f"task {t.id} belongs to {t.owner}")
                t.owner = new_owner
            elif status == "pending" and owner is None and t.owner == actor:
                t.owner = None  # releasing your own task
            elif owner is not None:
                t.owner = owner or None
            if status:
                t.status = status
            self._save(t)
            unblocked = []
            if t.status == "completed":
                unblocked = [
                    x
                    for x in self.all()
                    if x.status == "pending"
                    and x.blocked_by
                    and x.id not in ready_before
                    and not self.blockers(x)
                ]
            return t, unblocked

    def release(self, owner: str) -> list[Task]:
        with self.lock():
            out = []
            for t in self.all():
                if t.owner == owner and t.status == "in_progress":
                    t.status, t.owner = "pending", None
                    self._save(t)
                    out.append(t)
            return out


def fmt(store: TaskStore, t: Task) -> str:
    if t.status == "pending":
        b = store.blockers(t)
        state = f"blocked by {', '.join(b)}" if b else "ready"
    else:
        state = t.status
    owner = f" @{t.owner}" if t.owner else ""
    return f"#{t.id} [{state}]{owner} {t.subject}"


def setup(api: ExtensionAPI) -> None:
    store = TaskStore(api.paths.project / "tasks")
    api.services["tasks"] = store

    def create(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            t = store.create(args["subject"], args.get("description", ""), args.get("blocked_by"))
        except ValueError as e:
            return ToolResult.error(str(e))
        return ToolResult.text(f"Created {fmt(store, t)}", details=asdict(t))

    def update(args: dict, ctx: ToolContext) -> ToolResult:
        changes = {k: v for k, v in args.items() if k != "id"}
        try:
            t, unblocked = store.update(str(args["id"]), ctx.agent, **changes)
        except ValueError as e:
            return ToolResult.error(str(e))
        text = f"Updated {fmt(store, t)}"
        if unblocked:
            text += "\nNow ready: " + ", ".join(fmt(store, u) for u in unblocked)
        return ToolResult.text(text, details=asdict(t))

    def list_(args: dict, ctx: ToolContext) -> ToolResult:
        tasks = [
            t
            for t in store.all()
            if (not args.get("status") or t.status == args["status"])
            and (not args.get("owner") or t.owner == args["owner"])
        ]
        return ToolResult.text("\n".join(fmt(store, t) for t in tasks) or "(no tasks)")

    def get(args: dict, ctx: ToolContext) -> ToolResult:
        t = store.get(str(args["id"]))
        if t is None:
            return ToolResult.error(f"unknown task {args['id']}")
        return ToolResult.text(
            f"{fmt(store, t)}\n\n{t.description or '(no description)'}\nblocked_by: {t.blocked_by or '-'}"
        )

    roles = ("main", "teammate")
    api.register_tool(
        AgentTool(
            "task_create",
            "Create a durable task in the shared task graph (survives sessions; teammates can claim it). "
            "blocked_by lists existing task ids that must be completed first; task_update can change dependencies.",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "minLength": 1},
                    "description": {"type": "string"},
                    "blocked_by": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["subject"],
            },
            create,
            read_only=True,
        ),
        roles,
    )
    api.register_tool(
        AgentTool(
            "task_update",
            "Update a task. Setting status=in_progress claims it for you (refused if blocked or owned by someone "
            "else); status=completed reports which tasks became ready. "
            "Setting owner does not wake a teammate; team_message delivers work.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "owner": {"type": "string"},
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                    "add_blocked_by": {"type": "array", "items": {"type": "string"}},
                    "remove_blocked_by": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
            update,
            read_only=True,
        ),
        roles,
    )
    api.register_tool(
        AgentTool(
            "task_list",
            "List tasks with their state (ready / blocked / in_progress / completed) and owner.",
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "owner": {"type": "string"},
                },
            },
            list_,
            read_only=True,
        ),
        roles,
    )
    api.register_tool(
        AgentTool(
            "task_get",
            "Show one task with its full description.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            get,
            read_only=True,
        ),
        roles,
    )

    def cmd(session, args):
        if args.strip() == "clear":
            with store.lock():
                n = 0
                for t in store.all():
                    if t.status == "completed":
                        store._path(t.id).unlink(missing_ok=True)
                        n += 1
            api.ui.print(f"Removed {n} completed task(s).")
            return
        api.ui.print("\n".join(fmt(store, t) for t in store.all()) or "No tasks.")

    api.register_command(
        "tasks", cmd, "show the task graph (/tasks clear removes completed)", concurrent=True
    )
