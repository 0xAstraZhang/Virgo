"""Idle cost of the fullscreen frontend, status usage totals, and plain-REPL history import."""

from __future__ import annotations

import asyncio
import time

import pytest
from textual.css.query import DOMQuery
from textual.widgets import Static

from virgo.ai.types import AssistantMessage, TextContent, Usage, UserMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.editor import InputHistory, legacy_history
from virgo.harness.store import SessionStore


async def wait_for(pilot, predicate, *, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "UI did not reach the expected state"
        await pilot.pause(0.01)


# -- idle frames ------------------------------------------------------------------------


def test_idle_frames_neither_relayout_nor_walk_the_transcript(make_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            app.update_status()  # settle every label once
            updates, walks = [], []
            original_update, original_bool = Static.update, DOMQuery.__bool__
            monkeypatch.setattr(Static, "update", lambda self, *a, **kw: (updates.append(self.id), original_update(self, *a, **kw))[1])
            monkeypatch.setattr(DOMQuery, "__bool__", lambda self: (walks.append(1), original_bool(self))[1])
            for _ in range(20):
                app.last_status = 0  # also force the twice-a-second status refresh
                await app.poll()
            # Each Static.update relayouts every widget, and query truthiness matches every
            # widget: both made an idle long conversation use a full CPU core.
            assert updates == [] and walks == []
            session.permission_mode = "plan"
            app.update_status()
            assert "plan" in str(app.screen_stack[0].query_one("#status", Static).content)

    asyncio.run(scenario())


def test_activity_label_refresh_does_not_relayout(make_session, monkeypatch):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.1)
            card = await app.transcript._assistant("main")
            layouts = []
            original = Static.update
            monkeypatch.setattr(Static, "update", lambda self, content="", *, layout=True: (
                layouts.append(layout), original(self, content, layout=layout))[1])
            app.activity.begin(time.monotonic(), "medium")
            for _ in range(5):
                app.activity.tick()
                app.transcript.show_activity(app.activity.content)
                app.transcript.show_activity(app.activity.content)  # same frame twice: no second update
            assert layouts and not any(layouts)
            assert len(layouts) <= 5
            assert "Working" in card.thought_header.content.plain

    asyncio.run(scenario())


# -- status usage totals --------------------------------------------------------------------


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


# -- plain REPL history import ----------------------------------------------------------------

# Written by libedit's write_history_file (captured from this environment's editline readline).
LIBEDIT = (
    b"_HiStOrY_V2_\nplain\ntwo\\040\\040spaces\ntab\\011here\nback\\134slash\n"
    b"literal\\040\\134040\\040typed\nctrl\\^Achar\ndel\\^?char\n"
    b"\xe4\xb8\xad\xe6\x96\x87\\040\xe8\xbe\x93\xe5\x85\xa5\nline1\\012line2\ntrailing\\040backslash\\134\n"
)


def test_libedit_history_is_decoded(tmp_path):
    path = tmp_path / "history"
    path.write_bytes(LIBEDIT)
    assert legacy_history(path) == [
        "plain", "two  spaces", "tab\there", "back\\slash", "literal \\040 typed", "ctrl\x01char",
        "del\x7fchar", "中文 输入", "line1\nline2", "trailing backslash\\",
    ]


def test_gnu_readline_history_is_read_verbatim(tmp_path):
    path = tmp_path / "history"
    path.write_bytes(b"echo \\040 stays\nsecond entry\n\n")
    assert legacy_history(path) == ["echo \\040 stays", "second entry"]


def test_undecodable_history_does_not_stop_the_editor_from_starting(tmp_path):
    (tmp_path / "history").write_bytes(b"caf\xe9\nls\n")
    assert InputHistory(tmp_path).entries == ["caf�", "ls"]
    (tmp_path / "tui-history.jsonl").write_bytes(b'"ok"\n"bad \xff"\n')
    assert InputHistory(tmp_path).entries == ["ok", "bad �"]


def test_history_written_by_this_python_readline_round_trips(tmp_path):
    readline = pytest.importorskip("readline")
    entries = ["two  spaces", "tab\there", "back\\slash", "literal \\040 typed", "中文 ✓"]
    if "libedit" in (readline.__doc__ or ""):
        entries.append("line1\nline2")  # GNU readline stores entries one per line, unescaped
    readline.clear_history()
    try:
        for entry in entries:
            readline.add_history(entry)
        readline.write_history_file(str(tmp_path / "history"))
    finally:
        readline.clear_history()
    assert legacy_history(tmp_path / "history") == entries
