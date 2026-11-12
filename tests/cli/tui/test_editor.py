"""The input editor imports plain-REPL readline history."""

from __future__ import annotations

import pytest

from virgo.cli.tui.editor import InputHistory, legacy_history

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
