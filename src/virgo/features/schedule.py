"""Scheduled prompts: run a prompt later (``at``) or repeatedly (``cron``).

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
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..harness.extensions import ExtensionAPI

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step <= 0:
                raise ValueError("step must be positive")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
            if step != 1:
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"{part!r} out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
        try:
            self.sets = [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _RANGES, strict=True)]
        except ValueError as e:
            raise ValueError(f"invalid cron {expr!r}: {e}") from e
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


def parse_at(text: str, now: datetime | None = None) -> datetime:
    """Return a naive local time, the form the scheduler stores and compares."""
    now = now or datetime.now()
    m = re.fullmatch(r"\+?\s*(\d+)\s*([smhd])", text.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now + timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: n})
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError as e:
        raise ValueError(f"invalid time {text!r}: use ISO (2026-01-31T09:00) or +10m / +2h / +1d") from e
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

    def describe(self) -> str:
        when = f"cron '{self.cron}'" if self.cron else f"at {self.at}"
        return f"{self.id} {when}{' (persisted)' if self.persist else ''}: {self.prompt[:70]}"


class Scheduler:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.file = api.paths.project / "schedules.json"
        self.items: dict[str, Schedule] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def load(self) -> None:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except json.JSONDecodeError as e:
            self.api.ui.print(f"[schedules: ignoring corrupt {self.file}: {e}]", "error")
            return
        for d in data:
            try:
                s = Schedule(**d)
                if s.cron:
                    Cron(s.cron)
                if s.at:  # also repairs zone-qualified times saved by earlier versions
                    s.at = parse_at(str(s.at)).isoformat(timespec="seconds")
                self.items[s.id] = s
            except (TypeError, ValueError) as e:
                self.api.ui.print(f"[schedules: skipping invalid entry {d}: {e}]", "error")
        nums = [int(i[1:]) for i in self.items if i[1:].isdigit()]
        self._ids = itertools.count(max(nums, default=0) + 1)

    def save(self) -> None:
        data = [asdict(s) for s in self.items.values() if s.persist]
        if not data and not self.file.exists():
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.file)

    def add(self, prompt: str, cron: str | None, at: str | None, persist: bool) -> Schedule:
        if bool(cron) == bool(at):
            raise ValueError("give exactly one of cron or at")
        if cron:
            Cron(cron)
        at_iso = parse_at(at).isoformat(timespec="seconds") if at else None
        with self._lock:
            s = Schedule(f"s{next(self._ids)}", prompt, cron, at_iso, persist)
            self.items[s.id] = s
            self.save()
        self.start()
        return s

    def cancel(self, sid: str) -> bool:
        with self._lock:
            found = self.items.pop(sid, None)
            self.save()
        return found is not None

    def tick(self, now: datetime | None = None) -> list[Schedule]:
        now = now or datetime.now()
        key = now.strftime("%Y-%m-%dT%H:%M")
        due = []
        with self._lock:
            for s in list(self.items.values()):
                if s.cron and s.last_fired != key and _cron(s.cron).matches(now):
                    s.last_fired = key
                    due.append(s)
                elif s.at and datetime.fromisoformat(s.at) <= now:
                    self.items.pop(s.id, None)
                    due.append(s)
            if due:
                self.save()
        for s in due:
            self.api.notify(f"[Scheduled {s.id}] {s.prompt}", mode="follow_up", source="schedule")
        return due

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while not self._stop.wait(1.0):
                if self.items:
                    self.tick()

        self._thread = threading.Thread(target=loop, daemon=True, name="virgo-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def setup(api: ExtensionAPI) -> None:
    sched = Scheduler(api)
    api.services["schedule"] = sched

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        action = args["action"]
        if action == "list":
            return ToolResult.text("\n".join(s.describe() for s in sched.items.values()) or "(no schedules)")
        if action == "cancel":
            if not args.get("id"):
                return ToolResult.error("cancel needs id")
            return ToolResult.text("Cancelled." if sched.cancel(args["id"]) else f"No schedule {args['id']}.")
        if not args.get("prompt"):
            return ToolResult.error("add needs prompt")
        try:
            s = sched.add(args["prompt"], args.get("cron"), args.get("at"), bool(args.get("persist")))
        except ValueError as e:
            return ToolResult.error(str(e))
        nxt = _cron(s.cron).next_after(datetime.now()) if s.cron else None
        extra = f" Next run: {nxt.isoformat(timespec='minutes')}." if nxt else ""
        return ToolResult.text(
            f"Scheduled {s.describe()}.{extra} It runs as a new prompt while Virgo is open."
        )

    api.register_tool(
        AgentTool(
            "schedule",
            "Schedule a prompt to run later (at: ISO time or +10m/+2h/+1d) or repeatedly (cron: 5 fields, local "
            "time). When it fires, the prompt is delivered to you as a notification. Also list/cancel schedules.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "cancel"]},
                    "prompt": {"type": "string"},
                    "cron": {"type": "string"},
                    "at": {"type": "string"},
                    "persist": {"type": "boolean", "description": "Keep across Virgo sessions"},
                    "id": {"type": "string"},
                },
                "required": ["action"],
            },
            execute,
            read_only=True,
        ),
        roles=("main",),
    )

    def start(session):
        if not sched.items:
            sched.load()
        if sched.items:
            sched.start()

    api.on("session_start", start)
    api.on("session_shutdown", lambda s: sched.stop())
    api.register_command(
        "schedules",
        lambda s, a: api.ui.print("\n".join(x.describe() for x in sched.items.values()) or "No schedules."),
        "list scheduled prompts",
        concurrent=True,
    )
