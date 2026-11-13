"""Trashing and restoring a session updates late diagnostic/notification ownership."""

from __future__ import annotations

import pytest

from virgo.ai.types import UserMessage
from virgo.diagnostics import capture_error


@pytest.mark.parametrize("current", [True, False])
def test_trash_removes_late_notification_and_diagnostic_destination(make_session, trash, current):
    session = make_session(persist=True, refresh_models=False)
    original = session.main
    owner, path = original.store, original.store.path
    owner.append_message(UserMessage("saved conversation"))
    if not current:
        session.new_session()
    session.trash_session(path)
    retained_entries = list(owner.entries)
    pending = list(original.wakeups)
    evidence = capture_error("late background failure", phase="background.run")

    session.notify("late background completion", store=owner, diagnostics=(evidence,))
    session.retain_diagnostics(owner, (evidence,))

    assert session.runtime_for_store(owner) is None
    assert list(original.wakeups) == pending
    assert owner.entries == retained_entries
    assert original.diagnostic_access.get(evidence["error_id"]) is None
    assert not session.wakeups and not path.exists()
    assert session.runtime_for_store(session.store) is session.main


def test_failed_trash_keeps_owner_without_registering_unactivated_replacement(make_session, monkeypatch):
    session = make_session(persist=True, refresh_models=False)
    original, owner = session.main, session.store
    owner.append_message(UserMessage("keep this conversation"))
    owners_before = dict(session._main_runtimes)

    def fail(_path):
        raise PermissionError("trash unavailable")

    monkeypatch.setattr("virgo.harness.store.send2trash", fail)
    with pytest.raises(PermissionError, match="trash unavailable"):
        session.trash_session(owner.path)

    assert session._main_runtimes == owners_before
    assert session.runtime_for_store(owner) is original
    session.notify("still belongs here", store=owner)
    assert session.wakeups[-1].text().endswith("still belongs here\n</notification>")


def test_restored_session_rebinds_same_id_to_new_runtime(make_session, trash):
    session = make_session(persist=True, refresh_models=False)
    original, owner = session.main, session.store
    owner.append_message(UserMessage("restorable conversation"))
    path = owner.path
    session.new_session()
    active = session.main
    session.trash_session(path)
    assert session.runtime_for_store(owner) is None

    # Simulate an explicit restore from the user's trash, preserving the session
    # header identity. Opening it must not revive the deleted in-process Runtime.
    trash[-1][1].rename(path)
    session.open_session(path)
    restored = session.main
    assert restored is not original and restored.store.id == owner.id
    assert session.runtime_for_store(restored.store) is restored
    assert session.runtime_for_store(owner) is restored
    assert session.runtime_for_store(active.store) is active
    session.notify("completion for restored conversation", store=owner)
    assert "completion for restored conversation" in restored.wakeups[-1].text()
    assert not original.wakeups and not active.wakeups


@pytest.mark.parametrize("operation", ["notify", "retain_diagnostics"])
def test_deleted_runtime_cannot_receive_stale_lookup_delivery(make_session, monkeypatch, trash, operation):
    session = make_session(persist=True, refresh_models=False)
    original, owner = session.main, session.store
    owner.append_message(UserMessage("delete before delivery commits"))
    path = owner.path
    evidence = capture_error("late evidence", phase="background.run")
    lookup = session.runtime_for_store
    deleted = False
    after_delete = []

    def stale_lookup(store):
        nonlocal deleted
        runtime = lookup(store)
        if store is owner and not deleted:
            # Force the exact stale-lookup interleaving without thread timing.
            # The session RLock also permits this from reentrant extension code.
            deleted = True
            session.trash_session(path)
            after_delete.extend(owner.entries)
        return runtime

    monkeypatch.setattr(session, "runtime_for_store", stale_lookup)
    if operation == "notify":
        session.notify("late completion", store=owner, diagnostics=(evidence,))
    else:
        session.retain_diagnostics(owner, (evidence,))

    assert deleted and not path.exists()
    assert owner.entries == after_delete
    assert original.diagnostic_access.get(evidence["error_id"]) is None
    assert not original.wakeups and not session.wakeups
