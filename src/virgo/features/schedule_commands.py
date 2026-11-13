"""Manual schedule workflows, shared by fullscreen and plain terminals."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime

from ..harness.ui import UI, ScheduleChoice
from ..i18n import Translator
from .schedule import Cron, Schedule, Scheduler, parse_at, prepare_schedule

KINDS = ("once", "minutes", "hours", "daily", "weekly", "cron")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


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


def _required(value, t=None):
    t = t or Translator()
    if not value:
        raise ValueError(t("features.schedule.required"))
    return value


def _positive(value, t=None):
    t = t or Translator()
    if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
        raise ValueError(t("features.schedule.positive"))
    return int(value)


def _clock(value, t=None):
    t = t or Translator()
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError(t("features.schedule.clock_format"))
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(t("features.schedule.clock_range"))
    return hour, minute


def _future(value, t=None):
    t = t or Translator()
    when = parse_at(value, t=t)
    if when.timestamp() <= datetime.now().timestamp():
        raise ValueError(t("features.schedule.future"))
    return value


def _timing(ui, old: Schedule | None):
    keys = (["keep"] if old else []) + list(KINDS)
    labels = [ui.t("features.schedule.kind." + key) for key in keys]
    picked = _answer(
        ui.select(ui.t("features.schedule.kind_prompt", timezone=datetime.now().astimezone().tzinfo), labels)
    )
    kind = keys[labels.index(picked)]
    if kind == "keep":
        return {}
    if kind == "once":
        return {
            "at": _input(
                ui, ui.t("features.schedule.date_prompt"), validate=lambda value: _future(value, ui.t)
            )
        }
    if kind in {"minutes", "hours"}:
        n = _input(
            ui, ui.t("features.schedule.interval_prompt"), validate=lambda value: _positive(value, ui.t)
        )
        first = _input(
            ui,
            ui.t("features.schedule.first_prompt"),
            validate=lambda value: _future(value, ui.t) if value else None,
        )
        return {"interval_minutes": n * (60 if kind == "hours" else 1), "start_at": first}
    if kind in {"daily", "weekly"}:
        day = "*"
        if kind == "weekly":
            weekdays = [ui.t("features.schedule.weekday." + key) for key in WEEKDAYS]
            day = str(
                (weekdays.index(_answer(ui.select(ui.t("features.schedule.weekday_prompt"), weekdays))) + 1)
                % 7
            )
        hour, minute = _input(
            ui,
            ui.t("features.schedule.clock_prompt"),
            default="09:00",
            validate=lambda value: _clock(value, ui.t),
        )
        return {"cron": f"{minute} {hour} * * {day}"}

    def cron(value):
        Cron(value, t=ui.t)
        return value

    return {"cron": _input(ui, ui.t("features.schedule.cron_prompt"), validate=cron)}


def _details(schedule: Schedule, t=None) -> str:
    """Keep storage IDs out of the presentation while retaining full task content."""
    number = schedule.id.removeprefix("s")
    return schedule.details(t).replace(f"{schedule.id} · ", f"{number} · ", 1)


def _choice(schedule: Schedule, t=None) -> ScheduleChoice:
    t = t or Translator()
    kind, when = schedule.timing(t)
    frequency = kind if schedule.interval_minutes is not None or schedule.at else f"{kind} {when}"
    next_run = schedule.next_time(t)
    try:
        date = datetime.fromisoformat(next_run)
        next_run = date.strftime("%m/%d %H:%M" if date.year == datetime.now().year else "%Y/%m/%d %H:%M")
    except ValueError:
        next_run = t("features.schedule.no_match")
    return ScheduleChoice(schedule.id, schedule.display_title, frequency, next_run)


def _wizard(ui: UI, sched: Scheduler, old: Schedule | None = None) -> Schedule:
    title = _input(
        ui,
        ui.t("features.schedule.title_prompt"),
        default=old.display_title if old else "",
        validate=lambda value: _required(value, ui.t),
    )
    prompt = old.prompt if old else ""
    while True:
        prompt = _answer(ui.input_multiline(ui.t("features.schedule.content_prompt"), default=prompt))
        if prompt.strip():
            break
        ui.print(ui.t("features.schedule.content_required"), "error")
    while True:
        timing = _timing(ui, old)
        try:
            if old and not timing:
                preview = replace(old, title=title, prompt=prompt)
            else:
                preview = prepare_schedule(
                    prompt, persist=old.persist if old else True, title=title, t=ui.t, **timing
                )
                preview.id = old.id if old else ui.t("features.schedule.new")
            break
        except (ValueError, OverflowError, OSError) as error:
            ui.print(str(error), "error")
    info = _details(preview, ui.t)
    if timing.get("interval_minutes") and not timing.get("start_at"):
        info += "\n" + ui.t("features.schedule.estimated")
    save, cancel = ui.t("features.schedule.save"), ui.t("features.schedule.cancel")
    if _answer(ui.select_details(ui.t("features.schedule.save_question"), [save, cancel], info)) != save:
        raise _Cancelled
    if timing.get("at"):
        _future(timing["at"], ui.t)
    if old:
        saved = sched.edit(old.id, expected=old, title=title, prompt=prompt, **timing)
    else:
        saved = sched.add(prompt, title=title, persist=True, **timing)
    ui.print(ui.t("features.schedule.saved", title=saved.display_title, next_run=saved.next_time(ui.t)), "ok")
    return saved


def create_schedule(ui: UI, sched: Scheduler, args: str) -> str | None:
    if args.strip() or not ui.interactive:
        ui.command_result(ui.t("features.schedule.usage"))
        return
    try:
        with ui.schedule_workspace():
            return _wizard(ui, sched).id
    except _Cancelled:
        ui.print(ui.t("features.schedule.cancelled"), "dim")
    except (ValueError, TypeError, OverflowError, OSError) as error:
        ui.print(ui.t("features.schedule.not_saved", error=error), "error")


def manage_schedules(ui: UI, sched: Scheduler, args: str) -> None:
    if args.strip():
        ui.command_result(ui.t("features.schedule.usage"))
        return
    if not ui.interactive:
        rows = "\n".join(s.describe(ui.t) for s in sched.items.values()) or ui.t("features.schedule.empty")
        ui.command_result(
            ui.t("features.schedule.table") + "\n" + rows + "\n" + ui.t("features.schedule.usage")
        )
        return
    with ui.schedule_workspace():
        _manage_schedules(ui, sched)


def _manage_schedules(ui: UI, sched: Scheduler) -> None:
    selected = None
    while True:
        items = list(sched.items.values())
        choice = ui.select_schedule([_choice(s, ui.t) for s in items], selected=selected)
        # Explicit focus is a one-shot hint; subsequent navigation belongs to the UI.
        selected = None
        if choice is None:
            return
        if choice == "new":
            selected = create_schedule(ui, sched, "")
            continue
        sid = selected = choice
        try:
            while True:
                current = sched.get(sid)
                edit, delete, back = (ui.t("features.schedule." + key) for key in ("edit", "delete", "back"))
                action = ui.select_details(
                    ui.t("features.schedule.details_title"), [edit, delete, back], _details(current, ui.t)
                )
                if action in (None, back):
                    break
                if action == edit:
                    _wizard(ui, sched, current)
                elif (
                    ui.select_details(
                        ui.t("features.schedule.delete_question"),
                        [ui.t("features.schedule.cancel"), delete],
                        _details(current, ui.t),
                    )
                    == delete
                ):
                    ui.print(
                        ui.t("features.schedule.deleted" if sched.cancel(sid) else "features.schedule.gone")
                    )
                    break
        except _Cancelled:
            ui.print(ui.t("features.schedule.cancelled"), "dim")
        except (ValueError, TypeError, OverflowError, OSError) as error:
            ui.print(str(error), "error")
