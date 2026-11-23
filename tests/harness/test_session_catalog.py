"""Session discovery, stable workspace ordering, and empty-record cleanup."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from virgo.ai.types import UserMessage
from virgo.harness.settings import Paths
from virgo.harness.store import SessionStore, _session_info

from tests.support.harness import TORN_MESSAGE, write_legacy_session


def test_global_catalog_sorts_updates_and_keeps_workspace_creation_order(tmp_path):
    root = tmp_path / "sessions"
    older, newer = root / "older", root / "newer"
    paths = [older / "first.jsonl", newer / "second.jsonl", older / "third.jsonl"]
    write_legacy_session(paths[0], tmp_path / "older", "2025-01-01T12:00:00", "first\n  task")
    write_legacy_session(paths[1], tmp_path / "newer", "2025-02-01T12:00:00", "second task")
    write_legacy_session(paths[2], tmp_path / "older", "2025-03-01T12:00:00", "third task")
    for path, updated in zip(paths, [300, 200, 100], strict=True):
        os.utime(path, (updated, updated))

    items = SessionStore.list_all(root)
    assert [row["path"] for row in items] == paths
    assert items[0]["title"] == "first task"
    assert items[0]["cwd"] == tmp_path / "older"
    assert items[0]["messages"] == 1
    assert items[0]["workspace_created_at"] == items[2]["workspace_created_at"]
    assert items[0]["workspace_created_at"] < items[1]["workspace_created_at"]
    assert [row["path"] for row in SessionStore.list(older)] == [paths[0], paths[2]]
    assert not list(root.glob("*/.workspace.json")), "Catalog reads must not migrate files."

    # Updating a session reorders sessions without moving its workspace.
    os.utime(paths[2], (400, 400))
    updated_items = SessionStore.list_all(root)
    assert [row["path"] for row in updated_items] == [paths[2], paths[0], paths[1]]
    assert updated_items[0]["workspace_created_at"] == items[0]["workspace_created_at"]


def test_new_store_registers_workspace_before_first_message_and_keeps_marker(tmp_path):
    directory = tmp_path / "sessions" / "project"
    store = SessionStore.new(directory, tmp_path / "project")
    metadata = json.loads((directory / ".workspace.json").read_text())
    assert metadata["cwd"] == str(tmp_path / "project")
    assert not store.path.exists()
    row = store.describe()
    assert row["id"] == store.id and row["path"] == store.path
    assert row["messages"] == 0 and row["title"] == "(empty)"
    assert row["workspace_created_at"] == metadata["created_at"]

    store.append("model_change", model="faux/new")
    assert store.discard_if_empty()
    os.utime(directory, (42, 42))
    next_store = SessionStore.new(directory, store.cwd)
    next_store.append_message(UserMessage("next task"))
    assert next_store.describe()["workspace_created_at"] == metadata["created_at"]
    assert json.loads((directory / ".workspace.json").read_text()) == metadata


def test_first_write_migrates_legacy_workspace_using_oldest_creation(tmp_path):
    directory = tmp_path / "sessions"
    old_path = directory / "saved.jsonl"
    write_legacy_session(old_path, tmp_path, "2020-01-02T03:04:05")
    expected = datetime.fromisoformat("2020-01-02T03:04:05").timestamp()
    resumed = SessionStore.load(old_path)
    resumed.append_message(UserMessage("continued"))
    assert resumed.describe()["workspace_created_at"] == expected
    assert json.loads((directory / ".workspace.json").read_text())["created_at"] == expected
    original = old_path.read_text().splitlines()[0]
    assert json.loads(original)["timestamp"] == "2020-01-02T03:04:05"


@pytest.mark.parametrize("contents", ["{broken", "[]\n", '{}\n', '{"type":"session"}\nnull\n', '{"type":"session"}\n{"type":"message","message":null}\n'])
def test_corrupt_session_does_not_hide_other_workspaces(tmp_path, contents):
    root = tmp_path / "sessions"
    good_path = root / "good" / "saved.jsonl"
    write_legacy_session(good_path, tmp_path, "2020-01-01T00:00:00")
    bad = root / "bad"
    bad.mkdir()
    (bad / "corrupt.jsonl").write_text(contents)
    assert [row["path"] for row in SessionStore.list_all(root)] == [good_path]


def test_empty_catalog_is_read_only(tmp_path):
    missing = tmp_path / "missing"
    assert SessionStore.list_all(missing) == []
    assert SessionStore.list(missing) == []
    assert not missing.exists()


def test_continue_with_non_directory_sessions_path(make_session, project):
    directory = Paths(project).sessions
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.touch()
    store = make_session(session="continue").store
    assert store.path.parent == directory and not store.entries


def test_continue_skips_disappearing_candidate(make_session, project, monkeypatch):
    directory = Paths(project).sessions
    saved = directory / "saved.jsonl"
    write_legacy_session(saved, project, "2025-01-01")
    removed = directory / "removed.jsonl"
    removed.touch()
    iterdir = Path.iterdir

    def discard_after_listing(path):
        entries = list(iterdir(path))
        if path == directory:
            removed.unlink(missing_ok=True)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", discard_after_listing)
    assert make_session(session="continue").store.path == saved


def test_registration_failure_keeps_in_memory_catalog_available(tmp_path, monkeypatch):
    original = Path.open

    def reject_marker(path, *args, **kwargs):
        if path.name == ".workspace.json":
            raise PermissionError("read-only workspace metadata")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_marker)
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    store.append_message(UserMessage("saved without metadata"))
    assert SessionStore.list(store.path.parent)[0]["title"] == "saved without metadata"


@pytest.mark.parametrize("kind,data", [
    ("message", {"message": {"role": "user", "content": "hello"}}),
    ("compaction", {"summary": "earlier conversation"}),
    ("custom", {"kind": "goal", "data": {"condition": "finish"}}),
    ("custom", {"kind": "extension", "data": {}}),
    ("future_type", {}),
])
def test_cleanup_preserves_conversations_and_extension_state(tmp_path, kind, data):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    store.append(kind, **data)
    assert not store.discard_if_empty()
    assert store.path.exists()


def test_cleanup_removes_header_only_and_model_only_records(tmp_path):
    for suffix, entries in [
        ("header", []),
        ("model", [{"type": "model_change", "id": "change", "model": "faux/test", "parent_id": None}]),
        ("leaf", [{"type": "leaf", "id": "move", "parent_id": None, "target": None}]),
    ]:
        path = tmp_path / f"{suffix}.jsonl"
        header = {"type": "session", "version": 1, "id": suffix, "cwd": str(tmp_path)}
        path.write_text("\n".join(json.dumps(row) for row in [header, *entries]) + "\n")
        store = SessionStore.load(path)
        assert store.discard_if_empty()
        assert not path.exists()
        assert not store.discard_if_empty()
    assert not SessionStore.new(None, tmp_path).discard_if_empty()


def test_cleanup_rechecks_file_for_another_instances_messages(tmp_path):
    original = SessionStore.new(tmp_path / "sessions", tmp_path)
    original.append("model_change", model="faux/test")
    other = SessionStore.load(original.path)
    other.append_message(UserMessage("keep this new message"))
    assert not original.discard_if_empty()
    assert SessionStore.load(original.path).build_context()[0][0].text() == "keep this new message"


def test_cleanup_preserves_legacy_workspace_creation_when_oldest_record_is_empty(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir()
    empty = directory / "empty.jsonl"
    created = "2020-01-01T00:00:00"
    empty.write_text(json.dumps({
        "type": "session", "version": 1, "id": "empty", "cwd": str(tmp_path), "timestamp": created,
    }) + "\n")
    write_legacy_session(directory / "later.jsonl", tmp_path, "2025-01-01T00:00:00")
    expected = datetime.fromisoformat(created).timestamp()
    assert SessionStore.list(directory)[0]["workspace_created_at"] == expected
    assert not (directory / ".workspace.json").exists()

    assert SessionStore.load(empty).discard_if_empty()

    assert SessionStore.list(directory)[0]["workspace_created_at"] == expected
    assert json.loads((directory / ".workspace.json").read_text())["created_at"] == expected


def test_describe_in_memory_session_tracks_title_and_messages(tmp_path):
    store = SessionStore.new(None, tmp_path)
    created = store.describe()["mtime"]
    store.append_message(UserMessage("two\nlines"))
    row = store.describe()
    assert row["path"] is None
    assert row["title"] == "two lines" and row["messages"] == 1
    assert row["mtime"] >= created


@pytest.mark.parametrize("fragment", [
    pytest.param(TORN_MESSAGE, id="83-bytes"),
    pytest.param(b'{"type":"mess', id="short"),
    pytest.param(b'{"text":"\xe4\xb8', id="split-utf8"),
])
def test_catalog_reads_torn_tail_without_mutation(tmp_path, fragment):
    path = tmp_path / "sessions" / "workspace" / "saved.jsonl"
    write_legacy_session(path, tmp_path, "2025-01-01", "earlier task")
    original = path.read_bytes() + b"\r\n" + fragment
    path.write_bytes(original)
    mtime = path.stat().st_mtime_ns
    for _ in range(2):
        for row in (SessionStore.list(path.parent)[0], SessionStore.list_all(path.parent.parent)[0], _session_info(path)):
            assert (row["id"], row["title"], row["messages"]) == ("saved", "earlier task", 1)
        saved = SessionStore.load(path)
        assert saved.leaf == "user" and saved.build_context()[0][0].text() == "earlier task"
        assert path.read_bytes() == original and path.stat().st_mtime_ns == mtime

    # Empty-prefix cleanup also inspects from disk. The tail may contain user work.
    header = original.split(b"\n", 1)[0] + b"\n"
    path.write_bytes(header + fragment)
    mtime = path.stat().st_mtime_ns
    assert not SessionStore.load(path).discard_if_empty()
    assert path.read_bytes() == header + fragment and path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize(("has_header", "suffix", "number", "reason"), [
    pytest.param(False, b"", 1, "header", id="empty"),
    pytest.param(False, b" \t\r\n\n", 1, "header", id="blank-only"),
    pytest.param(False, b'\n{"type":"sess', 2, "header", id="truncated-header"),
    pytest.param(False, b'{"type":"custom","id":"other"}\n', 1, "header", id="wrong-header"),
    pytest.param(False, b'{"type":"session","cwd":"."}', 1, "id", id="missing-header-id"),
    pytest.param(False, b'{"type":"session","id":"s"}', 1, "cwd", id="missing-cwd"),
    pytest.param(True, b'{"type":"message","id":"bad","message":{"role":"assistant","usage":null}}', 4, "usage", id="bad-usage"),
    pytest.param(True, b'{"type":"message","id":"bad","message":{"role":"assistant","usage":{"input":true}}}', 4, "usage", id="bool-usage"),
    pytest.param(True, b'{"type":"message","id":"bad","message":{"role":"assistant","content":[{"type":"unknown"}]}}', 4, "message", id="bad-block"),
    pytest.param(True, b'{broken\n{}\n', 4, "JSON", id="interior-json"),
    pytest.param(True, b'{"id":"\xff"}\n', 4, "UTF-8", id="interior-utf8"),
    pytest.param(True, b'{broken\n', 4, "JSON", id="terminated"),
    pytest.param(True, b'{broken\r\n\n \t', 4, "JSON", id="terminated-before-blanks"),
    pytest.param(True, b'42', 4, "object", id="scalar"),
    pytest.param(True, b'{"type":"leaf","id":"bad","parent_id":"bad","target":null}', 4, "parent", id="parent-cycle"),
    pytest.param(True, b'[]\n', 4, "object", id="array"),
    pytest.param(True, b'{"type":"custom"}', 4, "id", id="missing-entry-id"),
    pytest.param(True, b'{"type":"message","id":"m"}', 4, "message", id="missing-message"),
    pytest.param(True, b'{"type":"message","id":"m","message":{}}', 4, "role", id="missing-role"),
    pytest.param(True, b'{"type":"message","id":"m","message":{"role":"other"}}', 4, "role", id="unknown-role"),
    pytest.param(True, b'{"type":"message","id":"m","message":{"role":"assistant","content":42}}', 4, "content", id="bad-content"),
    pytest.param(True, b'{"type":"leaf","id":"move"}', 4, "target", id="missing-target"),
    pytest.param(True, b'{"type":"leaf","id":"move","target":"unknown"}', 4, "target", id="unknown-target"),
])
def test_unrecoverable_session_is_reported_on_selection(make_session, project, has_header, suffix, number, reason):
    directory = Paths(project).sessions
    good, bad = directory / "older.jsonl", directory / "broken.jsonl"
    write_legacy_session(good, project, "2025-01-01")
    # Equal mtimes also exercise the catalog's path tiebreaker (broken < older).
    original = (good.read_bytes() + b"\r\n" if has_header else b"") + suffix
    bad.write_bytes(original)
    for path in (good, bad):
        os.utime(path, (100, 100))
    before = set(directory.iterdir())
    for items in (SessionStore.list(directory), SessionStore.list_all(directory.parent)):
        assert [item["path"] for item in items] == [good]
    for choice in (bad, "continue"):
        with pytest.raises(ValueError) as exc:
            make_session(session=choice)
        message = str(exc.value)
        assert str(bad) in message and f"line {number}" in message and reason.lower() in message.lower()
        assert "another saved session" in message.lower()
        assert bad.read_bytes() == original and set(directory.iterdir()) == before
