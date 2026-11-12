"""Session discovery, stable workspace ordering, and empty-record cleanup."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from virgo.ai.types import UserMessage
from virgo.harness.store import SessionStore

from tests.support.harness import write_legacy_session


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


def test_registration_failure_keeps_in_memory_catalog_available(tmp_path, monkeypatch):
    from pathlib import Path

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
