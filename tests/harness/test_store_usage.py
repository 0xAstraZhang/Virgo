"""Status usage totals count each saved entry once and follow branch switches."""

from __future__ import annotations

from virgo.ai.types import AssistantMessage, TextContent, Usage, UserMessage
from virgo.harness.store import SessionStore


def brute_force(store: SessionStore) -> dict:
    """The original full-branch computation."""
    totals = dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0)
    for entry in store.branch():
        message = entry.get("message", {})
        if message.get("role") == "assistant":
            usage = message["usage"]
        elif entry.get("kind") == "model_call":
            usage = entry["data"]["usage"]
        else:
            continue
        for key in totals:
            totals[key] += usage.get(key, 0)
    return totals


class CountingIndex(dict):
    """An entry index that records which ids a branch walk looks up."""

    def __init__(self, entries):
        super().__init__(entries)
        self.looked_up = []

    def get(self, key, default=None):
        self.looked_up.append(key)
        return super().get(key, default)


def test_total_usage_counts_only_new_entries_and_follows_branch_switches(project):
    store = SessionStore(None, project)
    reply = lambda n: AssistantMessage([TextContent("ok")], usage=Usage(n, 2 * n, 3 * n, 4 * n))  # noqa: E731
    store.append_message(UserMessage("start"))
    fork = store.append_message(reply(1))
    assert store.total_usage() == brute_force(store) == dict(input=1, output=2, cache_read=3, cache_write=4)
    store.append_message(reply(10))
    store.append("custom", kind="model_call", data={"usage": {"input": 100, "output": 0, "cache_read": 0, "cache_write": 0}})
    assert store.total_usage() == brute_force(store)

    store.by_id = index = CountingIndex(store.by_id)
    store.append_message(reply(1000))
    assert store.total_usage() == dict(input=1111, output=2022, cache_read=3033, cache_write=4044)
    assert len(index.looked_up) == 1  # only the new entry, not the whole branch
    index.looked_up.clear()
    assert store.total_usage()["input"] == 1111 and index.looked_up == []
    assert store.total_usage() == brute_force(store)

    store.leaf = fork  # switch to an earlier branch point (e.g. a branched conversation)
    assert store.total_usage() == brute_force(store) == dict(input=1, output=2, cache_read=3, cache_write=4)
    store.append_message(reply(5))
    assert store.total_usage() == brute_force(store) == dict(input=6, output=12, cache_read=18, cache_write=24)


def test_resumed_session_counts_its_whole_branch(project, tmp_path):
    store = SessionStore.new(tmp_path / "sessions", project)
    for n in (1, 2, 3):
        store.append_message(UserMessage(f"q{n}"))
        store.append_message(AssistantMessage([TextContent("a")], usage=Usage(n, n, n, n)))
    assert SessionStore.load(store.path).total_usage() == dict(input=6, output=6, cache_read=6, cache_write=6)
