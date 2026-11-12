"""Cron parsing and scheduled prompts that fire as notifications."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from virgo.ai.providers import faux
from virgo.features.schedule import Cron, parse_at

from tests.support.harness import RecordingUI


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


def test_schedule_converts_zone_qualified_times_to_local(make_session):
    # Models often send "...Z". Comparing that with the local clock raised inside the scheduler
    # thread, which then stopped firing every schedule.
    local = datetime(2026, 12, 24, 23, 30, tzinfo=UTC).astimezone().replace(tzinfo=None)
    assert parse_at("2026-12-24T23:30:00Z") == parse_at("2026-12-25T07:30:00+08:00") == local
    assert parse_at("2026-12-24T23:30") == datetime(2026, 12, 24, 23, 30)
    sched = make_session().services["schedule"]
    item = sched.add("check the build", None, "2026-12-24T23:30:00Z", False)
    assert item.at == local.isoformat(timespec="seconds")
    assert sched.tick(local - timedelta(minutes=1)) == []
    assert sched.tick(local) == [item] and not sched.items


def test_schedule_file_with_zone_qualified_or_invalid_times_is_repaired(make_session, project):
    (project / ".virgo").mkdir()
    (project / ".virgo" / "schedules.json").write_text(json.dumps([
        {"id": "s1", "prompt": "saved by an earlier version", "at": "2026-12-24T23:30:00+00:00", "persist": True},
        {"id": "s2", "prompt": "not a time", "at": "tomorrow", "persist": True},
        {"id": "s3", "prompt": "plain", "at": "2026-12-24T09:00:00", "persist": True},
    ]))
    ui = RecordingUI()
    sched = make_session(ui=ui).services["schedule"]
    local = datetime(2026, 12, 24, 23, 30, tzinfo=UTC).astimezone().replace(tzinfo=None)
    assert {k: v.at for k, v in sched.items.items()} == {
        "s1": local.isoformat(timespec="seconds"), "s3": "2026-12-24T09:00:00",
    }
    assert "skipping invalid entry" in ui.text
    assert sched.tick(datetime(2026, 1, 1)) == []  # ticking no longer raises on the saved entry


def test_live_workspace_schedule_ownership_survives_changes_and_shutdown(make_session, project, tmp_path):
    """Exercise the real tool, storage, session hooks and notification delivery."""
    when = "2099-01-01T12:00:00"
    first = make_session()
    second = make_session()
    other_project = tmp_path / "other"
    other_project.mkdir()
    other = make_session(cwd=other_project)

    def tool(session, **args):
        faux.script("test", faux.reply(tools=[("schedule", args)]), faux.reply("done"))
        session.prompt("manage schedule")

    tool(first, action="add", prompt="shared first", at=when, persist=True)
    tool(second, action="add", prompt="shared second", at=when, persist=True)
    tool(second, action="add", prompt="local second", at=when)
    tool(other, action="add", prompt="other workspace", at=when, persist=True)
    first_sched, second_sched = first.services["schedule"], second.services["schedule"]
    assert {s.prompt for s in first_sched.items.values()} == {"shared first", "shared second"}
    assert {s.prompt for s in second_sched.items.values()} == {"shared first", "shared second", "local second"}
    saved_file = project / ".virgo" / "schedules.json"
    assert {s["prompt"] for s in json.loads(saved_file.read_text(encoding="utf-8"))} == {"shared first", "shared second"}
    sid = next(s.id for s in second_sched.items.values() if s.prompt == "shared first")
    tool(second, action="cancel", id=sid)
    assert sid not in first_sched.items
    tool(first, action="add", prompt="transfer on shutdown", at=when, persist=True)
    tool(first, action="add", prompt="discard on shutdown", at=when)
    first.shutdown()
    # A new main runtime must not reload an already owned schedule file.
    second.new_session()
    second_sched.tick(datetime.fromisoformat(when))
    first_sched.tick(datetime.fromisoformat(when))
    assert len(second.wakeups) == 3
    assert not first.wakeups and not other.wakeups
    assert {s.prompt for s in other.services["schedule"].items.values()} == {"other workspace"}
    assert json.loads(saved_file.read_text(encoding="utf-8")) == []
    second.shutdown()
    restarted = make_session()
    assert not restarted.services["schedule"].items
