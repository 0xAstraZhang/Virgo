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
