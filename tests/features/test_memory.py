"""Memory saves, deduplicates, indexes, and refuses to rewrite malformed records."""

from __future__ import annotations

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage

from tests.support.harness import calls, tool_results


def test_memory_save_dedupe_and_index(make_session, project, tmp_path):
    s = make_session(
        faux.reply(
            tools=[
                (
                    "memory",
                    {
                        "action": "save",
                        "name": "Prefers tabs",
                        "type": "feedback",
                        "description": "indentation",
                        "body": "Use tabs.",
                    },
                ),
                (
                    "memory",
                    {
                        "action": "save",
                        "name": "Stack",
                        "type": "project",
                        "description": "tech stack",
                        "body": "FastAPI + Postgres",
                    },
                ),
            ]
        ),
        faux.reply(
            tools=[
                (
                    "memory",
                    {
                        "action": "save",
                        "name": "Tabs again",
                        "type": "feedback",
                        "description": "x",
                        "body": "use   tabs.",
                    },
                )
            ]
        ),
        faux.reply("ok"),
    )
    s.prompt("remember")
    r = tool_results(s)
    assert "Saved" in r[0].text() and "Saved" in r[1].text() and "Already saved" in r[2].text()
    user_dir = tmp_path / "home" / "memory"
    assert (user_dir / "prefers-tabs.md").exists() and (project / ".virgo" / "memory" / "stack.md").exists()
    assert "Prefers tabs" in (user_dir / "MEMORY.md").read_text()
    s2 = make_session(faux.reply("ok"))
    s2.prompt("hi")
    assert "Prefers tabs" in calls()[0]["system"] and "FastAPI" not in calls()[0]["system"]  # index only


def test_memory_tidy_rejects_malformed_records_before_rewriting(make_session):
    session = make_session(faux.reply('[{"name": "a", "type": "user", "description": "d", "body": 3}]'))
    store = session.services["memory"]
    store.save("one", "user", "first", "alpha")
    store.save("two", "user", "second", "beta")
    session.prompt("/memory tidy")
    assert "Tidy skipped" in session.ui.text
    assert {m.name for m in store.all()} == {"one", "two"}


def test_memory_update_with_unknown_type_on_disk_returns_a_clear_error(make_session):
    session = make_session(
        faux.reply(tools=[("memory", {"action": "update", "name": "odd", "description": "changed"})]),
        faux.reply("done"),
    )
    store = session.services["memory"]
    folder = store.dirs["project"]
    folder.mkdir(parents=True, exist_ok=True)
    original = "---\nname: odd\ndescription: old\ntype: weird\n---\n\nbody\n"
    (folder / "odd.md").write_text(original, encoding="utf-8")
    session.prompt("update the memory")
    result = next(m for m in session.main.messages if isinstance(m, ToolResultMessage))
    assert result.is_error
    assert "unknown type 'weird'" in result.text()
    assert (folder / "odd.md").read_text(encoding="utf-8") == original
