"""Recoverable session deletion and protection against later writes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from virgo.ai.types import UserMessage
from virgo.harness.store import SessionStore

from .test_session_catalog import write_legacy


@pytest.fixture
def trash(tmp_path, monkeypatch):
    directory = tmp_path / "trash"
    directory.mkdir()
    moved = []

    def move(path):
        source = Path(path)
        destination = directory / source.name
        source.rename(destination)
        moved.append((source, destination))

    monkeypatch.setattr("virgo.harness.store.send2trash", move)
    return moved


def test_trash_preserves_complete_file_and_stops_later_writes(tmp_path, trash):
    store = SessionStore.new(tmp_path / "sessions", tmp_path)
    store.append_message(UserMessage("保留完整对话"))
    store.append("custom", kind="extension", data={"saved": True})
    path = store.path
    original = path.read_bytes()
    marker = (path.parent / ".workspace.json").read_bytes()

    store.trash()

    assert not path.exists()
    assert store.path is None
    assert len(trash) == 1 and trash[0][0] == path
    assert trash[0][1].read_bytes() == original
    assert (path.parent / ".workspace.json").read_bytes() == marker
    assert SessionStore.list(path.parent) == []
    store.append_message(UserMessage("late extension write"))
    assert not path.exists()
    assert trash[0][1].read_bytes() == original
    store.trash()
    assert len(trash) == 1


def test_trash_failure_keeps_active_session_and_all_cached_stores(make_session, monkeypatch):
    session = make_session(persist=True)
    first = session.store
    first.append_message(UserMessage("keep this conversation"))
    path = first.path
    session.open_session(path)
    second, runtime = session.store, session.main
    original = path.read_bytes()

    def fail(_path):
        raise PermissionError("Trash is unavailable")

    monkeypatch.setattr("virgo.harness.store.send2trash", fail)
    with pytest.raises(PermissionError, match="Trash is unavailable"):
        session.trash_session(path)

    assert session.store is second and session.main is runtime
    assert first.path == second.path == path
    assert path.read_bytes() == original
    second.append_message(UserMessage("still writable"))
    assert SessionStore.load(path).build_context()[0][-1].text() == "still writable"


def test_replacement_runtime_failure_keeps_current_session_out_of_trash(make_session, trash):
    session = make_session(persist=True)
    store, runtime = session.store, session.main
    store.append_message(UserMessage("keep this conversation"))
    path = store.path
    original = path.read_bytes()
    session.paths.project.mkdir(parents=True, exist_ok=True)
    (session.paths.project / "SYSTEM.md").write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        session.trash_session(path)

    assert not trash
    assert session.store is store and session.main is runtime
    assert store.path == path and path.read_bytes() == original


def test_absolute_delete_matches_session_reopened_with_relative_path(make_session, monkeypatch, trash):
    session = make_session(persist=True)
    first = session.store
    first.append_message(UserMessage("relative-path conversation"))
    path = first.path
    monkeypatch.chdir(path.parent)
    session.open_session(Path(path.name))
    second, runtime = session.store, session.main
    assert second.path == path

    session.trash_session(path)

    assert len(trash) == 1 and trash[0][0] == path
    assert first.path is None and second.path is None
    assert session.store is not second and session.main is not runtime
    assert session.store.path is not None and session.store.path != path
    assert not session.main.messages
    for store in (first, second):
        store.append("custom", kind="late", data={})
    session.store.append_message(UserMessage("new conversation"))
    assert not path.exists()
    assert SessionStore.load(session.store.path).build_context()[0][0].text() == "new conversation"


@pytest.mark.parametrize("current", [True, False])
def test_trashing_reopened_session_detaches_all_visited_stores(make_session, trash, current):
    session = make_session(persist=True)
    first = session.store
    first.append_message(UserMessage("old conversation"))
    path = first.path
    session.open_session(path)
    second = session.store
    if not current:
        session.new_session()
    previous, runtime = session.store, session.main

    session.trash_session(path)

    assert first.path is None and second.path is None
    assert len(trash) == 1 and trash[0][0] == path
    assert session.cwd == first.cwd
    assert session.store.path is not None and session.store.path != path
    if current:
        assert session.store is not previous and session.main is not runtime
        assert not session.main.messages
    else:
        assert session.store is previous and session.main is runtime
    for store in (first, second):
        store.append("custom", kind="late", data={})
    session.store.append_message(UserMessage("new conversation"))
    session.shutdown()
    assert not path.exists()
    assert SessionStore.load(session.store.path).build_context()[0][0].text() == "new conversation"


def test_trashing_unsaved_current_session_keeps_new_session_persistent(make_session, trash):
    session = make_session(persist=True)
    old = session.store
    path = old.path
    assert not path.exists()

    session.trash_session(path)

    assert not trash and old.path is None
    assert session.store is not old
    assert session.store.path is not None and session.store.path != path
    assert session.main.store is session.store and not session.main.messages
    session.new_session()
    session.store.append_message(UserMessage("saved after deleting empty session"))
    assert session.store.path.exists()
    assert not path.exists()


def test_trashing_oldest_legacy_session_preserves_workspace_order(tmp_path, trash):
    directory = tmp_path / "sessions"
    oldest = directory / "oldest.jsonl"
    latest = directory / "latest.jsonl"
    write_legacy(oldest, tmp_path, "2020-01-02T03:04:05")
    write_legacy(latest, tmp_path, "2025-01-02T03:04:05")
    expected = datetime.fromisoformat("2020-01-02T03:04:05").timestamp()
    assert not (directory / ".workspace.json").exists()

    SessionStore.load(oldest).trash()

    assert len(trash) == 1 and trash[0][0] == oldest
    assert json.loads((directory / ".workspace.json").read_text())["created_at"] == expected
    remaining = SessionStore.list(directory)
    assert [item["path"] for item in remaining] == [latest]
    assert remaining[0]["workspace_created_at"] == expected


def test_trashing_other_workspace_does_not_switch_current_session(make_session, tmp_path, trash):
    session = make_session(persist=True)
    current, runtime = session.store, session.main
    other_cwd = tmp_path / "other-workspace"
    other_cwd.mkdir()
    other = SessionStore.new(session.paths.user / "sessions" / "other", other_cwd)
    other.append_message(UserMessage("other workspace conversation"))
    path = other.path
    original = path.read_bytes()

    session.trash_session(path)

    assert len(trash) == 1 and trash[0][0] == path
    assert trash[0][1].read_bytes() == original
    assert not path.exists()
    assert session.store is current and session.main is runtime
    assert session.cwd == current.cwd
