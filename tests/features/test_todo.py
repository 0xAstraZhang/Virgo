"""The checklist records model-chosen progress without driving the agent loop."""

from __future__ import annotations

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import UserMessage

from tests.support.harness import calls, tool_results


def test_parallel_todos_have_no_size_cap_or_stop_continuation(make_session):
    todos = [{"content": f"task {i}", "status": "in_progress"} for i in range(60)]
    session = make_session(
        faux.reply(tools=[("todo_write", {"todos": todos})]), faux.reply("waiting for results"),
    )
    session.prompt("track parallel work")
    result, = tool_results(session)
    assert not result.is_error and result.details["todos"] == todos
    assert "(0/60 completed)" in result.text()
    assert len(calls()) == 2
    assert not any(isinstance(m, UserMessage) and m.kind == "continue" for m in session.main.messages)


@pytest.mark.parametrize("invalid", [
    [{"content": "new", "status": "pending"}, {"content": "  ", "status": "pending"}],
    [{"content": "new", "status": "unknown"}],
    [None],
    "not a list",
])
def test_invalid_todos_leave_previous_state_intact(make_session, invalid):
    original = [{"content": "keep", "status": "pending"}]
    session = make_session(
        faux.reply(tools=[("todo_write", {"todos": original})]),
        faux.reply(tools=[("todo_write", {"todos": invalid})]), faux.reply("done"),
    )
    session.prompt("record work")
    assert tool_results(session)[1].is_error
    assert session.services["todos"]["todos"] == original


def test_todos_replace_restore_and_display(make_session):
    old = [{"content": "obsolete", "status": "pending"}]
    current = [{"content": "keep", "status": "completed"}, {"content": "next", "status": "in_progress"}]
    session = make_session(
        faux.reply(tools=[("todo_write", {"todos": old})]),
        faux.reply(tools=[("todo_write", {"todos": current})]), faux.reply("done"), persist=True,
    )
    session.prompt("record work")
    restored = make_session(session=session.store.path)
    assert restored.services["todos"]["todos"] == current
    restored.prompt("/todos")
    assert "[x] keep" in restored.ui.text and "[~] next" in restored.ui.text
    assert "obsolete" not in restored.ui.text
    assert calls() == []
    faux.push("test", faux.reply(tools=[("todo_write", {"todos": []})]), faux.reply("cleared"))
    restored.prompt("clear list")
    assert restored.services["todos"]["todos"] == []
