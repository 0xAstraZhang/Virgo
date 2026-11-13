"""Manual schedule workflows, shared by fullscreen and plain terminals."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime

from ..harness.ui import UI, ScheduleChoice
from .schedule import Cron, Schedule, Scheduler, parse_at, prepare_schedule

KINDS = ["单次", "每 n 分钟", "每 n 小时", "每天", "每周", "自定义 cron"]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
USAGE = "/schedule — 创建任务；/schedules — 查看、编辑、删除任务（不接受参数）。"


class _Cancelled(Exception):
    pass


def _answer(value):
    if value is None:
        raise _Cancelled
    return value


def _input(ui, question, *, default="", validate=None):
    while True:
        value = _answer(ui.input(question, default=default)).strip()
        try:
            return validate(value) if validate else value
        except (ValueError, OverflowError, OSError) as error:
            ui.print(str(error), "error")


def _required(value):
    if not value:
        raise ValueError("此项不能为空。")
    return value


def _positive(value):
    if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
        raise ValueError("请输入正整数。")
    return int(value)


def _clock(value):
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError("请输入 HH:MM，例如 09:00。")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("时间须在 00:00–23:59 之间。")
    return hour, minute


def _future(value):
    when = parse_at(value)
    if when.timestamp() <= datetime.now().timestamp():
        raise ValueError("首次执行时间必须在未来。")
    return value


def _timing(ui, old: Schedule | None):
    choices = (["保持原定时规则"] if old else []) + KINDS
    kind = _answer(ui.select(f"定时类别 · 系统时区 {datetime.now().astimezone().tzinfo}", choices))
    if kind == "保持原定时规则":
        return {}
    if kind == "单次":
        return {"at": _input(ui, "执行日期时间（YYYY-MM-DD HH:MM，或 +10m）", validate=_future)}
    if kind in {"每 n 分钟", "每 n 小时"}:
        n = _input(ui, "间隔 n（正整数）", validate=_positive)
        first = _input(ui, "首次日期时间（留空：保存后一个间隔；YYYY-MM-DD HH:MM）",
                       validate=lambda value: _future(value) if value else None)
        return {"interval_minutes": n * (60 if kind == "每 n 小时" else 1), "start_at": first}
    if kind in {"每天", "每周"}:
        day = "*"
        if kind == "每周":
            day = str((WEEKDAYS.index(_answer(ui.select("星期几", WEEKDAYS))) + 1) % 7)
        hour, minute = _input(ui, "执行时刻（HH:MM）", default="09:00", validate=_clock)
        return {"cron": f"{minute} {hour} * * {day}"}

    def cron(value):
        Cron(value)
        return value

    return {"cron": _input(ui, "Cron（分 时 日 月 星期；系统本地时间）", validate=cron)}


def _details(schedule: Schedule) -> str:
    """Keep storage IDs out of the presentation while retaining full task content."""
    number = schedule.id.removeprefix("s")
    return schedule.details().replace(f"{schedule.id} · ", f"{number} · ", 1)


def _choice(schedule: Schedule) -> ScheduleChoice:
    kind, when = schedule.timing()
    frequency = kind if schedule.interval_minutes is not None or schedule.at else f"{kind} {when}"
    next_run = schedule.next_time()
    try:
        date = datetime.fromisoformat(next_run)
        next_run = date.strftime("%m/%d %H:%M" if date.year == datetime.now().year else "%Y/%m/%d %H:%M")
    except ValueError:
        next_run = "未来一年无匹配时间"
    return ScheduleChoice(schedule.id, schedule.display_title, frequency, next_run)


def _wizard(ui: UI, sched: Scheduler, old: Schedule | None = None) -> Schedule:
    title = _input(ui, "任务标题", default=old.display_title if old else "", validate=_required)
    prompt = old.prompt if old else ""
    while True:
        prompt = _answer(ui.input_multiline("任务内容（支持多行）", default=prompt))
        if prompt.strip():
            break
        ui.print("任务内容不能为空。", "error")
    while True:
        timing = _timing(ui, old)
        try:
            if old and not timing:
                preview = replace(old, title=title, prompt=prompt)
            else:
                preview = prepare_schedule(prompt, persist=old.persist if old else True, title=title, **timing)
                preview.id = old.id if old else "新任务"
            break
        except (ValueError, OverflowError, OSError) as error:
            ui.print(str(error), "error")
    info = _details(preview)
    if timing.get("interval_minutes") and not timing.get("start_at"):
        info += "\n首次时间为预估，实际从保存时开始计时。"
    if _answer(ui.select_details("保存定时任务？", ["保存", "取消"], info)) != "保存":
        raise _Cancelled
    if timing.get("at"):
        _future(timing["at"])
    if old:
        saved = sched.edit(old.id, expected=old, title=title, prompt=prompt, **timing)
    else:
        saved = sched.add(prompt, title=title, persist=True, **timing)
    ui.print(f"已保存 {saved.display_title}\n下次执行: {saved.next_time()}", "ok")
    return saved


def create_schedule(ui: UI, sched: Scheduler, args: str) -> str | None:
    if args.strip() or not ui.interactive:
        ui.command_result(USAGE)
        return
    try:
        with ui.schedule_workspace():
            return _wizard(ui, sched).id
    except _Cancelled:
        ui.print("已取消，未保存变更。", "dim")
    except (ValueError, TypeError, OverflowError, OSError) as error:
        ui.print(f"未保存任务: {error}", "error")


def manage_schedules(ui: UI, sched: Scheduler, args: str) -> None:
    if args.strip():
        ui.command_result(USAGE)
        return
    if not ui.interactive:
        ui.command_result("ID │ 类别 │ 时间 │ 标题\n" + ("\n".join(s.describe() for s in sched.items.values()) or "暂无任务。") + "\n" + USAGE)
        return
    with ui.schedule_workspace():
        _manage_schedules(ui, sched)


def _manage_schedules(ui: UI, sched: Scheduler) -> None:
    selected = None
    while True:
        items = list(sched.items.values())
        choice = ui.select_schedule([_choice(s) for s in items], selected=selected)
        if choice is None:
            return
        if choice == "new":
            selected = create_schedule(ui, sched, "") or selected
            continue
        sid = selected = choice
        try:
            while True:
                current = sched.get(sid)
                action = ui.select_details("任务详情", ["编辑", "删除", "返回列表"], _details(current))
                if action in (None, "返回列表"):
                    break
                if action == "编辑":
                    _wizard(ui, sched, current)
                elif ui.select_details("确认删除此任务？", ["取消", "删除"], _details(current)) == "删除":
                    ui.print("已删除。" if sched.cancel(sid) else "任务已不存在。")
                    break
        except _Cancelled:
            ui.print("已取消，未保存变更。", "dim")
        except (ValueError, TypeError, OverflowError, OSError) as error:
            ui.print(str(error), "error")
