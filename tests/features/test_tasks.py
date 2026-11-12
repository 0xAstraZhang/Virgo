"""The task graph: dependencies, ownership, claiming, and safe task ids."""

from __future__ import annotations

import json

import pytest

from virgo.ai.providers import faux
from virgo.features.tasks import TaskStore

from tests.support.harness import tool_results


def test_task_graph(project):
    st = TaskStore(project / ".virgo" / "tasks")
    a = st.create("design")
    b = st.create("build", blocked_by=[a.id])
    c = st.create("ship", blocked_by=[b.id])
    with pytest.raises(ValueError, match="cycle"):
        st.update(a.id, "main", add_blocked_by=[c.id])
    with pytest.raises(ValueError, match="blocked"):
        st.update(b.id, "alice", status="in_progress")
    st.update(a.id, "alice", status="in_progress")
    with pytest.raises(ValueError, match="belongs to alice"):
        st.update(a.id, "bob", status="in_progress")
    _, unblocked = st.update(a.id, "alice", status="completed")
    assert [t.id for t in unblocked] == [b.id]
    claimed = st.claim_next("bob")
    assert claimed.id == b.id and claimed.owner == "bob"
    assert st.release("bob")[0].status == "pending"


def test_task_tools(make_session):
    s = make_session(
        # Tool calls in one response run in parallel; create the prerequisite first.
        faux.reply(tools=[("task_create", {"subject": "one"})]),
        faux.reply(tools=[("task_create", {"subject": "two", "blocked_by": ["1"]})]),
        faux.reply(tools=[("task_update", {"id": "1", "status": "in_progress"})]),
        faux.reply(tools=[("task_update", {"id": "1", "status": "completed"}), ("task_list", {})]),
        faux.reply("ok"),
    )
    s.prompt("plan")
    r = tool_results(s)
    assert "#2 [blocked by 1]" in r[1].text()
    assert "@main" in r[2].text()
    assert "Now ready: #2" in r[3].text()


def test_task_ids_cannot_name_files_outside_the_task_directory(project):
    store = TaskStore(project / ".virgo" / "tasks")
    store.create("real")
    (project / ".virgo" / "secret.json").write_text(json.dumps({"id": "x", "subject": "outside"}))
    assert store.get("../secret") is None
    with pytest.raises(ValueError):
        store.update("../secret", "main", status="completed")
    with pytest.raises(ValueError, match="unknown task"):
        store.create("dependent", blocked_by=["../secret"])
