"""The todo list validates progress and reminds the agent about unfinished items."""

from __future__ import annotations

from virgo.ai.providers import faux
from virgo.ai.types import UserMessage

from tests.support.harness import tool_results


def test_todo_validation_and_stop_reminder(make_session):
    s = make_session(
        faux.reply(
            tools=[
                (
                    "todo_write",
                    {
                        "todos": [
                            {"content": "a", "status": "in_progress"},
                            {"content": "b", "status": "in_progress"},
                        ]
                    },
                )
            ]
        ),
        faux.reply(
            tools=[
                (
                    "todo_write",
                    {
                        "todos": [
                            {"content": "a", "status": "in_progress"},
                            {"content": "b", "status": "pending"},
                        ]
                    },
                )
            ]
        ),
        faux.reply("I'm done"),
        faux.reply("ok, finishing b"),
    )
    s.prompt("go")
    r = tool_results(s)
    assert r[0].is_error and "only one" in r[0].text()
    assert "[~] a" in r[1].text() and "(0/2 completed)" in r[1].text()
    # stopping with open items triggers exactly one reminder
    reminders = [m for m in s.main.messages if isinstance(m, UserMessage) and m.kind == "continue"]
    assert len(reminders) == 1 and "unfinished" in reminders[0].text()
    assert s.main.agent.last_text == "ok, finishing b"
