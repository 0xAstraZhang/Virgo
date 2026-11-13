"""Scheduled prompts: run once (``at``), by calendar (``cron``), or at elapsed intervals.

Cron uses 5 fields (minute hour day-of-month month day-of-week; ``*``, ``*/n``, ``a-b``, ``a-b/n``,
lists; day-of-week 0-7 with 0/7 = Sunday; when both day fields are restricted either may match).
``at`` takes an ISO time or a relative offset like ``+10m``, ``+2h``, ``+1d``. Times are local;
one with a zone or UTC offset (``...Z``, ``+08:00``) is converted to local time.
Schedules fire only while Virgo runs; missed times are not replayed. ``persist`` keeps a schedule
across sessions in ``.virgo/schedules.json``.
"""

from __future__ import annotations

import functools
import itertools
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..harness.extensions import ExtensionAPI
from ..i18n import Translator

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _parse_field(spec: str, lo: int, hi: int, t=None) -> set[int]:
    t = t or Translator()
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step <= 0:
                raise ValueError(t("features.schedule.cron_step"))
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
            if step != 1:
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(t("features.schedule.cron_range", part=part, lo=lo, hi=hi))
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str, t=None):
        t = t or Translator()
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(t("features.schedule.cron_fields"))
        try:
            self.sets = [_parse_field(f, lo, hi, t) for f, (lo, hi) in zip(fields, _RANGES, strict=True)]
        except ValueError as e:
            raise ValueError(t("features.schedule.cron_invalid", expression=expr, error=e)) from e
        if 7 in self.sets[4]:
            self.sets[4].add(0)
        self.dom_any = fields[2] == "*"
        self.dow_any = fields[4] == "*"

    def matches(self, t: datetime) -> bool:
        minute, hour, dom, month, dow = self.sets
        if t.minute not in minute or t.hour not in hour or t.month not in month:
            return False
        d_ok, w_ok = t.day in dom, ((t.weekday() + 1) % 7) in dow
        if self.dom_any or self.dow_any:
            return d_ok and w_ok
        return d_ok or w_ok

    def next_after(self, t: datetime, limit_days: int = 366) -> datetime | None:
        cur = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = t + timedelta(days=limit_days)
        while cur < end:
            if self.matches(cur):
                return cur
            cur += timedelta(minutes=1)
        return None


@functools.lru_cache(maxsize=128)
def _cron(expr: str) -> Cron:
    """Parsed expressions are immutable; the scheduler checks them every second."""
    return Cron(expr)


def parse_at(text: str, now: datetime | None = None, *, t=None) -> datetime:
    """Return a naive local time, the form the scheduler stores and compares."""
    t = t or Translator()
    now = now or datetime.now()
    m = re.fullmatch(r"\+?\s*(\d+)\s*([smhd])", text.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now + timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: n})
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError as e:
        raise ValueError(t("features.schedule.invalid_time", time=text)) from e
    # A zone or offset ("...Z", "+08:00") names an instant: convert it rather than read it as local time.
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


@dataclass
class Schedule:
    id: str
    prompt: str
    cron: str | None = None
    at: str | None = None  # ISO timestamp
    persist: bool = False
    created: float = field(default_factory=time.time)
    last_fired: str | None = None
    title: str = ""
    interval_minutes: int | None = None
    start_at: float | None = None  # interval anchor, epoch seconds (elapsed time, including DST)
    next_run: float | None = None

    @property
    def display_title(self) -> str:
        return self.title or next(
            (line.strip() for line in self.prompt.splitlines() if line.strip()), "Untitled"
        )

    def timing(self, t=None) -> tuple[str, str]:
        t = t or Translator()
        if self.interval_minutes is not None:
            n = self.interval_minutes
            kind = (
                t("features.schedule.hours", count=n // 60)
                if n % 60 == 0
                else t("features.schedule.minutes", count=n)
            )
            return kind, datetime.fromtimestamp(self.next_run).isoformat(sep=" ", timespec="seconds")
        if self.at:
            return t("features.schedule.kind.once"), self.at.replace("T", " ")
        minute, hour, dom, month, dow = self.cron.split()
        if minute.isdecimal() and hour.isdecimal() and dom == month == "*":
            clock = f"{int(hour):02}:{int(minute):02}"
            if dow == "*":
                return t("features.schedule.kind.daily"), clock
            if dow.isdecimal():
                day = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")[
                    int(dow) % 7
                ]
                return t("features.schedule.kind.weekly"), f"{t('features.schedule.weekday.' + day)} {clock}"
        return t("features.schedule.kind.cron"), self.cron

    def next_time(self, t=None) -> str:
        t = t or Translator()
        if self.interval_minutes is not None:
            return datetime.fromtimestamp(self.next_run).astimezone().isoformat(timespec="seconds")
        if self.at:
            return self.at
        nxt = _cron(self.cron).next_after(datetime.now())
        return nxt.isoformat(timespec="minutes") if nxt else t("features.schedule.no_match")

    def details(self, t=None) -> str:
        t = t or Translator()
        kind, when = self.timing(t)
        anchor = (
            t(
                "features.schedule.anchor",
                time=datetime.fromtimestamp(self.start_at).astimezone().isoformat(timespec="seconds"),
            )
            if self.interval_minutes is not None
            else ""
        )
        return t(
            "features.schedule.details",
            id=self.id,
            title=self.display_title,
            kind=kind,
            when=when,
            anchor=anchor,
            next_run=self.next_time(t),
            timezone=datetime.now().astimezone().tzinfo,
            persist=t("features.schedule.yes" if self.persist else "features.schedule.no"),
            prompt=self.prompt,
        )

    def advance(self, now: float) -> None:
        """Advance from the anchor, never from completion time or the last poll."""
        seconds = self.interval_minutes * 60
        self.next_run = self.start_at + (math.floor((now - self.start_at) / seconds) + 1) * seconds

    def describe(self, t=None) -> str:
        kind, when = self.timing(t)
        return f"{self.id} │ {kind} │ {when} │ {self.display_title}"


def prepare_schedule(
    prompt: str,
    cron: str | None = None,
    at: str | None = None,
    persist: bool = False,
    *,
    title: str = "",
    interval_minutes: int | None = None,
    start_at: str | None = None,
    t=None,
) -> Schedule:
    """Validate an entire draft before changing any shared or persisted state."""
    t = t or Translator()
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(t("features.schedule.content_empty"))
    if not isinstance(title, str) or "\n" in title or "\r" in title:
        raise ValueError(t("features.schedule.title_single"))
    if sum((bool(cron), bool(at), interval_minutes is not None)) != 1:
        raise ValueError(t("features.schedule.one_rule"))
    s = Schedule("", prompt, cron, None, persist, title=title.strip())
    if cron:
        Cron(cron, t=t)
    if at:
        s.at = parse_at(at, t=t).isoformat(timespec="seconds")
    if interval_minutes is not None:
        if type(interval_minutes) is not int or interval_minutes <= 0:
            raise ValueError(t("features.schedule.interval_positive"))
        s.interval_minutes = interval_minutes
        now = time.time()
        s.start_at = parse_at(start_at, t=t).timestamp() if start_at else now + interval_minutes * 60
        if s.start_at <= now:
            raise ValueError(t("features.schedule.future"))
        s.next_run = s.start_at
        datetime.fromtimestamp(s.next_run)  # reject unrepresentable dates before saving
    elif start_at is not None:
        raise ValueError(t("features.schedule.start_interval"))
    return s


@dataclass
class _WorkspaceSchedules:
    items: dict[str, Schedule] = field(default_factory=dict)
    owners: dict[str, Scheduler] = field(default_factory=dict)
    members: list[Scheduler] = field(default_factory=list)
    ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))


# A TUI can retain several AgentSessions in one workspace. Serialize their
# schedule mutations and keep a single delivery owner for each persisted entry.
_workspace_lock = threading.RLock()
_workspaces: dict[Path, _WorkspaceSchedules] = {}


class Scheduler:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.file = (api.paths.project / "schedules.json").resolve()
        self._workspace: _WorkspaceSchedules | None = None
        self._lock = _workspace_lock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def items(self) -> dict[str, Schedule]:
        with self._lock:
            if self._workspace is None or self._stop.is_set():
                return {}
            return {
                sid: s
                for sid, s in self._workspace.items.items()
                if s.persist or self._workspace.owners[sid] is self
            }

    def load(self) -> None:
        with self._lock:
            if self._workspace is not None or self._stop.is_set():
                return
            workspace = _workspaces.get(self.file)
            if workspace is None:
                workspace = _workspaces[self.file] = _WorkspaceSchedules()
                self._workspace = workspace
                workspace.members.append(self)
                self._load_file()
            else:
                self._workspace = workspace
                workspace.members.append(self)

    def _load_file(self) -> None:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except json.JSONDecodeError as e:
            self.api.ui.print(
                self.api.session.t("features.schedule.corrupt", path=self.file, error=e), "error"
            )
            return
        advanced = False
        for d in data:
            try:
                s = Schedule(**d)
                if not isinstance(s.prompt, str) or not s.prompt.strip() or not isinstance(s.title, str):
                    raise ValueError(self.api.session.t("features.schedule.invalid_content"))
                if sum((bool(s.cron), bool(s.at), s.interval_minutes is not None)) != 1:
                    raise ValueError(self.api.session.t("features.schedule.invalid_rule"))
                if s.cron:
                    Cron(s.cron)
                if s.at:  # also repairs zone-qualified times saved by earlier versions
                    s.at = parse_at(str(s.at)).isoformat(timespec="seconds")
                if s.interval_minutes is not None:
                    if type(s.interval_minutes) is not int or s.interval_minutes <= 0:
                        raise ValueError(self.api.session.t("features.schedule.invalid_interval"))
                    for value in (s.start_at, s.next_run):
                        if not isinstance(value, (float, int)) or not math.isfinite(value):
                            raise ValueError(self.api.session.t("features.schedule.invalid_timestamp"))
                        datetime.fromtimestamp(value)
                    if s.next_run < s.start_at:
                        raise ValueError(self.api.session.t("features.schedule.invalid_anchor"))
                    if s.next_run <= time.time():
                        s.advance(time.time())
                        datetime.fromtimestamp(s.next_run)
                        advanced = True
                self._workspace.items[s.id] = s
                self._workspace.owners[s.id] = self
            except (TypeError, ValueError, OverflowError, OSError) as e:
                self.api.ui.print(
                    self.api.session.t("features.schedule.invalid_entry", entry=d, error=e), "error"
                )
        nums = [int(i[1:]) for i in self.items if i[1:].isdigit()]
        self._workspace.ids = itertools.count(max(nums, default=0) + 1)
        if advanced:
            self.save()

    def save(self) -> None:
        data = [asdict(s) for s in self._workspace.items.values() if s.persist]
        if not data and not self.file.exists():
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.file)

    def add(
        self,
        prompt: str,
        cron: str | None = None,
        at: str | None = None,
        persist: bool = False,
        *,
        title: str = "",
        interval_minutes: int | None = None,
        start_at: str | None = None,
    ) -> Schedule:
        s = prepare_schedule(
            prompt,
            cron,
            at,
            persist,
            title=title,
            interval_minutes=interval_minutes,
            start_at=start_at,
            t=self.api.session.t,
        )
        with self._lock:
            if self._stop.is_set():
                raise ValueError(self.api.session.t("features.schedule.closed"))
            s.id = f"s{next(self._workspace.ids)}"
            self._workspace.items[s.id] = s
            self._workspace.owners[s.id] = self
            try:
                self.save()
            except OSError:
                self._workspace.items.pop(s.id)
                self._workspace.owners.pop(s.id)
                raise
        self.start()
        return s

    def get(self, sid: str) -> Schedule:
        with self._lock:
            if sid not in self.items:
                raise ValueError(self.api.session.t("features.schedule.missing", id=sid))
            return replace(self.items[sid])

    def edit(self, sid: str, *, expected: Schedule | None = None, **changes) -> Schedule:
        t = self.api.session.t
        with self._lock:
            old = self.get(sid)
            # A poll can advance next_run/last_fired while a form is open. Only
            # reject a conflicting user edit, not ordinary scheduler progress.
            fields = ("prompt", "title", "cron", "at", "persist", "interval_minutes", "start_at")
            if expected is not None and any(getattr(old, k) != getattr(expected, k) for k in fields):
                raise ValueError(t("features.schedule.changed"))
            unknown = changes.keys() - set(fields)
            if unknown:
                raise ValueError(t("features.schedule.unknown_fields", fields=", ".join(sorted(unknown))))
            timing = {"cron", "at", "interval_minutes", "start_at"}
            if timing & changes.keys():
                draft = prepare_schedule(
                    changes.get("prompt", old.prompt),
                    changes.get("cron"),
                    changes.get("at"),
                    changes.get("persist", old.persist),
                    title=changes.get("title", old.title),
                    interval_minutes=changes.get("interval_minutes"),
                    start_at=changes.get("start_at"),
                    t=t,
                )
                draft.id, draft.created = old.id, old.created
                # Editing an equivalent cron/at rule does not reset duplicate suppression.
                if (draft.cron, draft.at) == (old.cron, old.at) and old.interval_minutes is None:
                    draft.last_fired = old.last_fired
            else:
                draft = replace(old, **changes)
                if not isinstance(draft.prompt, str) or not draft.prompt.strip():
                    raise ValueError(t("features.schedule.content_empty"))
                if not isinstance(draft.title, str) or "\n" in draft.title or "\r" in draft.title:
                    raise ValueError(t("features.schedule.title_single"))
                draft.title = draft.title.strip()
            self._workspace.items[sid] = draft
            try:
                self.save()
            except OSError:
                self._workspace.items[sid] = old
                raise
            return replace(draft)

    def cancel(self, sid: str) -> bool:
        with self._lock:
            if sid not in self.items:
                return False
            found = self._workspace.items.pop(sid)
            owner = self._workspace.owners.pop(sid)
            try:
                self.save()
            except OSError:
                self._workspace.items[sid] = found
                self._workspace.owners[sid] = owner
                raise
        return found is not None

    def tick(self, now: datetime | None = None) -> list[Schedule]:
        now = now or datetime.now()
        key = now.strftime("%Y-%m-%dT%H:%M")
        due = []
        with self._lock:
            for s in self.items.values():
                if self._workspace.owners[s.id] is not self:
                    continue
                if s.interval_minutes is not None and s.next_run <= now.timestamp():
                    s.advance(now.timestamp())
                    due.append(s)
                elif s.cron and s.last_fired != key and _cron(s.cron).matches(now):
                    s.last_fired = key
                    due.append(s)
                elif s.at and datetime.fromisoformat(s.at) <= now:
                    self._workspace.items.pop(s.id)
                    self._workspace.owners.pop(s.id)
                    due.append(s)
            if due:
                self.save()
        for s in due:
            self.api.notify(
                self.api.session.t("features.schedule.notify", id=s.id, prompt=s.prompt),
                mode="follow_up",
                source="schedule",
            )
        return due

    def start(self) -> None:
        if self._stop.is_set() or (self._thread and self._thread.is_alive()):
            return

        def loop():
            while not self._stop.wait(1.0):
                if self.items:
                    self.tick()

        self._thread = threading.Thread(target=loop, daemon=True, name="virgo-scheduler")
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            workspace = self._workspace
            if workspace is None:
                return
            workspace.members.remove(self)
            for sid, owner in list(workspace.owners.items()):
                if owner is not self:
                    continue
                if workspace.items[sid].persist and workspace.members:
                    workspace.owners[sid] = workspace.members[0]
                else:
                    workspace.items.pop(sid)
                    workspace.owners.pop(sid)
            if not workspace.members:
                _workspaces.pop(self.file, None)


def setup(api: ExtensionAPI) -> None:
    sched = Scheduler(api)
    api.services["schedule"] = sched

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        action = args["action"]
        try:
            if action == "list":
                rows = "\n".join(s.describe(ctx.t) for s in sched.items.values()) or ctx.t(
                    "features.schedule.list_empty"
                )
                return ToolResult.text(rows + ctx.t("features.schedule.get_hint"))
            if action in {"get", "edit", "cancel"} and not args.get("id"):
                raise ValueError(ctx.t("features.schedule.needs_id", action=action))
            if action == "get":
                return ToolResult.text(sched.get(args["id"]).details(ctx.t))
            if action == "cancel":
                return ToolResult.text(
                    ctx.t("features.schedule.cancel_result")
                    if sched.cancel(args["id"])
                    else ctx.t("features.schedule.not_found", id=args["id"])
                )
            changes = {k: v for k, v in args.items() if k not in {"action", "id"}}
            if action == "edit":
                s = sched.edit(args["id"], **changes)
            elif action == "add":
                if not args.get("prompt"):
                    raise ValueError(ctx.t("features.schedule.add_prompt"))
                s = sched.add(**changes)
            else:
                raise ValueError(ctx.t("features.schedule.unknown_action", action=action))
            return ToolResult.text(
                ctx.t(
                    "features.schedule.scheduled",
                    description=s.describe(ctx.t),
                    next_run=s.next_time(ctx.t),
                    id=s.id,
                )
            )
        except (ValueError, TypeError, OverflowError, OSError) as e:
            return ToolResult.error(str(e))

    api.register_tool(
        AgentTool(
            "schedule",
            "Schedule a prompt to run later (at: ISO time or +10m/+2h/+1d) or repeatedly (cron: 5 fields, local "
            "time), or every interval_minutes elapsed minutes (optional start_at: first ISO date/time). "
            "Specify exactly one of at, cron, interval_minutes. get reads full details; list returns titles. "
            "edit preserves timing when only title/prompt changes; to change timing provide a complete new rule. "
            "Missed intervals are skipped on restart. Tasks run only while Virgo is open.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "get", "edit", "cancel"]},
                    "prompt": {"type": "string"},
                    "title": {"type": "string", "description": "Short, single-line task title"},
                    "interval_minutes": {"type": "integer", "minimum": 1},
                    "start_at": {
                        "type": "string",
                        "description": "First interval execution (ISO, future); default now + interval",
                    },
                    "cron": {"type": "string"},
                    "at": {"type": "string"},
                    "persist": {"type": "boolean", "description": "Keep across Virgo sessions"},
                    "id": {"type": "string"},
                },
                "required": ["action"],
            },
            execute,
            read_only=True,
            i18n_key="tools.schedule",
        ),
        roles=("main",),
    )

    def start(session):
        sched.load()
        # Even an empty member may inherit persisted work when its owner closes.
        sched.start()

    api.on("session_start", start)
    api.on("session_shutdown", lambda s: sched.stop())
    from .schedule_commands import create_schedule, manage_schedules

    api.register_command(
        "schedule",
        lambda s, a: create_schedule(api.ui, sched, a),
        "create a scheduled task",
        description_key="features.commands.schedule",
    )
    api.register_command(
        "schedules",
        lambda s, a: manage_schedules(api.ui, sched, a),
        "browse and manage scheduled tasks",
        description_key="features.commands.schedules",
    )
