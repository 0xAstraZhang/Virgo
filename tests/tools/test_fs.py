"""File tools: read paging, write, edit matching, glob and grep."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from virgo.agent.types import ToolContext
from virgo.tools import EDIT, GLOB, GREP, READ, WRITE
from virgo.tools.fs import _prepare_edit, make_file_tool
from virgo.tools.output import MAX_LINES

from tests.support.execution import file_policy


def ctx(cwd: Path) -> ToolContext:
    return ToolContext("c1", cwd, threading.Event())


def edit(cwd, path, *pairs):
    args = _prepare_edit({"path": path, "edits": [{"old_text": o, "new_text": n} for o, n in pairs]})
    return EDIT.execute(args, ctx(cwd))


def test_read_pages_large_files(project):
    f = project / "big.txt"
    f.write_text("".join(f"line {i}\n" for i in range(1, MAX_LINES + 501)), newline="\n")
    r = READ.execute({"path": "big.txt"}, ctx(project))
    assert r.as_text().startswith("1|line 1\n2|line 2\n")
    assert f"Use offset={MAX_LINES + 1}" in r.as_text()
    r2 = READ.execute({"path": "big.txt", "offset": MAX_LINES + 1, "limit": 3}, ctx(project))
    assert r2.as_text().startswith(f"{MAX_LINES + 1}|line {MAX_LINES + 1}\n")
    assert READ.execute({"path": "missing.txt"}, ctx(project)).is_error


def test_read_line_numbered_output_stays_within_byte_budget(project):
    from virgo.tools.output import MAX_BYTES

    f = project / "wide.txt"
    f.write_text("".join(f"{'x' * 120}\n" for _ in range(600)), newline="\n")
    result = READ.execute({"path": "wide.txt"}, ctx(project))
    assert not result.is_error
    assert len(result.as_text().encode("utf-8")) <= MAX_BYTES
    assert result.as_text().startswith("1|")
    assert "Use offset=" in result.as_text()


@pytest.mark.parametrize("tool,arguments", [
    (READ, {"path": "secret.txt"}),
    (GREP, {"path": ".", "pattern": "private"}),
    (GLOB, {"path": ".", "pattern": "*"}),
])
def test_file_tools_enforce_read_scope(project, tool, arguments):
    outside = project.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private-value")
    arguments = {**arguments, "path": str(outside / arguments["path"])}
    policy = file_policy(project)
    wrapped = make_file_tool(tool, lambda _ctx: policy)
    denied = wrapped.execute(arguments, ctx(project))
    assert denied.is_error and denied.details["boundary"]["dimension"] == "read_paths"
    assert "private-value" not in denied.as_text()
    full_read = file_policy(project, read_paths=(project.parent,))
    allowed = make_file_tool(tool, lambda _ctx: full_read).execute(arguments, ctx(project))
    assert not allowed.is_error, allowed.as_text()


@pytest.mark.parametrize("tool", [READ, GREP])
def test_scoped_read_cannot_follow_external_symlink(project, tool):
    outside = project.parent / "secret.txt"
    outside.write_text("private-value")
    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating file symlinks is unavailable")
    policy = file_policy(project)
    arguments = {"path": "link.txt", **({"pattern": "private"} if tool is GREP else {})}
    result = make_file_tool(tool, lambda _ctx: policy).execute(arguments, ctx(project))
    assert result.is_error and "private-value" not in result.as_text()


def test_file_tools_keep_private_storage_hidden_and_full_scope_writes_explicit(project):
    private = project.parent / "home"
    secret = private / "secret.txt"
    secret.write_text("private-value")
    policy = file_policy(project, read_paths=(project.parent,), write_paths=(project.parent,), private_paths=(private,))
    read = make_file_tool(READ, lambda _ctx: policy)
    denied = read.execute({"path": str(secret)}, ctx(project))
    assert denied.is_error and denied.details["boundary"]["dimension"] == "protected_path"
    target = project.parent / "outside.txt"
    write = make_file_tool(WRITE, lambda _ctx: policy)
    result = write.execute({"path": str(target), "content": "allowed"}, ctx(project))
    assert not result.is_error and target.read_text() == "allowed", result.as_text()
    readonly = file_policy(project, read_paths=(project.parent,))
    denied = make_file_tool(WRITE, lambda _ctx: readonly).execute(
        {"path": str(target), "content": "denied"}, ctx(project))
    assert denied.is_error and target.read_text() == "allowed"


def test_write_creates_parents(project):
    r = WRITE.execute({"path": "a/b/c.txt", "content": "hi"}, ctx(project))
    assert not r.is_error and (project / "a/b/c.txt").read_text() == "hi"


def test_edit_unique_multi_and_errors(project):
    f = project / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\na = 1\n")
    assert "2 places" in edit(project, "m.py", ("a = 1", "a = 9")).as_text()
    r = edit(project, "m.py", ("b = 2", "b = 3"), ("c = 3\na = 1", "c = 4\na = 1"))
    assert not r.is_error, r.as_text()
    assert f.read_text() == "a = 1\nb = 3\nc = 4\na = 1\n"
    assert "not found" in edit(project, "m.py", ("zzz", "y")).as_text()
    assert "overlap" in edit(project, "m.py", ("b = 3\nc", "x"), ("c = 4", "y")).as_text()


def test_edit_tolerates_quotes_whitespace_and_keeps_crlf(project):
    f = project / "q.txt"
    f.write_bytes("say “hello”   \r\nnext\r\n".encode())
    r = edit(project, "q.txt", ('say "hello"\nnext', "say hi\nnext"))
    assert not r.is_error, r.as_text()
    assert f.read_bytes() == b"say hi\r\nnext\r\n"


def test_edit_whitespace_match_consumes_the_newline_old_text_ends_with(project):
    f = project / "geo.py"
    f.write_text("import os\n\ndef area(w, h):   \n    return w * h\n\ndef main():\n    pass\n")
    # The model dropped the trailing spaces, so only the whitespace-tolerant match applies.
    r = edit(project, "geo.py", ("def area(w, h):\n    return w * h\n", "def rect_area(w, h):\n    return w * h\n"))
    assert not r.is_error, r.as_text()
    assert f.read_text() == "import os\n\ndef rect_area(w, h):\n    return w * h\n\ndef main():\n    pass\n"
    # Without a trailing newline in old_text the line ending is left alone, also at the end of a file.
    assert not edit(project, "geo.py", ("def main():\n    pass  ", "def main():\n    return 0")).is_error
    assert f.read_text().endswith("\ndef main():\n    return 0\n")
    f.write_text("a = 1  \nb = 2")
    assert not edit(project, "geo.py", ("a = 1\nb = 2\n", "a = 3\n")).is_error
    assert f.read_text() == "a = 3\n"


def test_edit_accepts_legacy_argument_shape(project):
    (project / "x.txt").write_text("foo\n")
    args = _prepare_edit({"path": "x.txt", "oldText": "foo", "newText": "bar"})
    assert not EDIT.execute(args, ctx(project)).is_error
    assert (project / "x.txt").read_text() == "bar\n"


def test_glob_and_grep(project):
    (project / "src").mkdir()
    (project / "src" / "a.py").write_text("def hello():\n    return 1\n")
    (project / "b.txt").write_text("hello world\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "skip.py").write_text("hello")
    g = GLOB.execute({"pattern": "**/*.py"}, ctx(project)).as_text()
    assert str(Path("src/a.py")) in g and "skip.py" not in g
    out = GREP.execute({"pattern": "hello", "glob": "*.py"}, ctx(project)).as_text()
    assert out == f"{Path('src/a.py')}:1:def hello():"
    assert (
        "b.txt:1:hello world"
        in GREP.execute({"pattern": "HELLO", "ignore_case": True}, ctx(project)).as_text()
    )


def test_glob_patterns_respect_path_segments(project):
    for name in ("main.py", "src/top.py", "src/pkg/a.py", "src/pkg/sub/c.py", "src/pkg/notes.md", "docs/a.py"):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text("value = 1\n")

    def found(pattern, **kw):
        out = GLOB.execute({"pattern": pattern, **kw}, ctx(project)).as_text()
        return set() if out == "(no matches)" else {Path(line).as_posix() for line in out.splitlines()}

    nested = {"src/top.py", "src/pkg/a.py", "src/pkg/sub/c.py"}
    assert found("src/**/*.py") == nested  # `**/` also matches no directory at all
    assert found("src/*.py") == {"src/top.py"}  # `*` does not cross a directory boundary
    assert found("src/*/*.py") == {"src/pkg/a.py"}
    assert found("**/*.py") == found("*.py") == nested | {"main.py", "docs/a.py"}
    assert found("**/pkg/*.py") == {"src/pkg/a.py"}
    assert found("src/**") == nested | {"src/pkg/notes.md"}
    assert found("**/*.py", path="src/pkg") == {"src/pkg/a.py", "src/pkg/sub/c.py"}
    assert found("src/*.md") == set()

    def searched(glob):
        out = GREP.execute({"pattern": "value", "glob": glob}, ctx(project)).as_text()
        return {Path(line.split(":", 1)[0]).as_posix() for line in out.splitlines()}

    assert searched("**/*.py") == nested | {"main.py", "docs/a.py"}  # root-level files were skipped
    assert searched("src/**/*.py") == nested
    assert searched("src/*.py") == {"src/top.py"}
    assert searched("*.md") == {"src/pkg/notes.md"}


def test_glob_and_grep_respect_gitignore_and_include_ignored(project):
    (project / ".git" / "info").mkdir(parents=True)
    (project / ".git" / "info" / "exclude").write_text("excluded.txt\n")
    (project / ".git" / "config").write_text("needle\n")
    (project / ".gitignore").write_text(
        "# comment\n"
        "*.log\n"
        "!important.log\n"
        "/root_only.txt\n"
        "generated/\n"
        "docs/**/*.tmp\n"
    )
    files = {
        "keep.py": "needle\n",
        "debug.log": "needle\n",
        "important.log": "needle\n",
        "root_only.txt": "needle\n",
        "sub/root_only.txt": "needle\n",
        "sub/.gitignore": "local_skip.py\n",
        "sub/local_skip.py": "needle\n",
        "sub/local_keep.py": "needle\n",
        "generated/out.py": "needle\n",
        "docs/a.tmp": "needle\n",
        "docs/nested/b.tmp": "needle\n",
        "excluded.txt": "needle\n",
        "dist/bundle.js": "needle\n",
    }
    for rel, content in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def glob_set(**kwargs):
        out = GLOB.execute({"pattern": "**/*", **kwargs}, ctx(project)).as_text()
        return {Path(line).as_posix() for line in out.splitlines()}

    default_files = glob_set()
    assert {"keep.py", "important.log", "sub/root_only.txt", "sub/local_keep.py"} <= default_files
    for ignored in ("debug.log", "root_only.txt", "sub/local_skip.py", "generated/out.py",
                    "docs/a.tmp", "docs/nested/b.tmp", "excluded.txt", "dist/bundle.js", ".git/config"):
        assert ignored not in default_files

    # Subdirectory search still inherits ancestor .gitignore rules.
    sub_files = glob_set(path="sub")
    assert "sub/root_only.txt" in sub_files and "sub/local_keep.py" in sub_files
    assert "sub/local_skip.py" not in sub_files

    # include_ignored=True exposes gitignored and default-ignored dirs while still skipping .git.
    all_files = glob_set(include_ignored=True)
    for ignored in ("debug.log", "root_only.txt", "sub/local_skip.py", "generated/out.py",
                    "docs/a.tmp", "docs/nested/b.tmp", "excluded.txt", "dist/bundle.js"):
        assert ignored in all_files
    assert ".git/config" not in all_files

    # grep mirrors default .gitignore filtering and include_ignored opt-in.
    grep_default = GREP.execute({"pattern": "needle", "mode": "files"}, ctx(project)).as_text().splitlines()
    assert set(Path(p).as_posix() for p in grep_default) == {
        "keep.py", "important.log", "sub/root_only.txt", "sub/local_keep.py",
    }
    grep_all = GREP.execute(
        {"pattern": "needle", "mode": "files", "include_ignored": True}, ctx(project)
    ).as_text().splitlines()
    assert "generated/out.py" in {Path(p).as_posix() for p in grep_all}
    assert ".git/config" not in {Path(p).as_posix() for p in grep_all}

    # Explicit single-file grep still searches a file even if .gitignore ignores it.
    direct = GREP.execute({"pattern": "needle", "path": "debug.log"}, ctx(project)).as_text()
    assert direct == "debug.log:1:needle"


def test_grep_modes_and_fixed_strings(project):
    (project / "a.py").write_text("foo(bar[0])\nfoo(bar[0]) + 1\nother\n")
    (project / "b.py").write_text("foo(bar[0])\n")
    (project / "c.py").write_text("foo_regex_only\n")

    # fixed_strings=True matches regex metacharacters literally.
    lit = GREP.execute({"pattern": "foo(bar[0])", "fixed_strings": True, "mode": "files"}, ctx(project))
    assert not lit.is_error
    assert [Path(p).as_posix() for p in lit.as_text().splitlines()] == ["a.py", "b.py"]
    assert lit.details == {"files": 2, "mode": "files"}

    # mode="count" reports per-file match line counts and total in details.
    counted = GREP.execute({"pattern": "foo(bar[0])", "fixed_strings": True, "mode": "count"}, ctx(project))
    assert not counted.is_error
    assert counted.as_text().splitlines() == ["a.py:2", "b.py:1"]
    assert counted.details == {"files": 2, "matches": 3, "mode": "count"}

    # limit in files/count mode caps files and appends the file-limit notice.
    limited = GREP.execute({"pattern": "foo", "mode": "files", "limit": 2}, ctx(project))
    lines = limited.as_text().splitlines()
    assert len(lines) == 3 and "stopped after 2 files" in lines[-1]

    # Invalid mode returns a clear error.
    bad = GREP.execute({"pattern": "foo", "mode": "bogus"}, ctx(project))
    assert bad.is_error and "Invalid grep mode" in bad.as_text()


def test_gitignore_rule_edge_cases():
    from virgo._gitignore import matches_scopes, parse_gitignore

    base = Path("/repo")
    rules = parse_gitignore(
        "\\#literal-hash\n"
        "\\!literal-bang\n"
        "trailing\\ \n"
        "a/**/b\n"
        "cache/**\n"
        "*.[oa]\n"
        "![z].o\n"
    )
    scopes = [(base, rules)]
    assert matches_scopes(base / "#literal-hash", False, scopes)
    assert matches_scopes(base / "!literal-bang", False, scopes)
    assert matches_scopes(base / "trailing ", False, scopes)
    assert not matches_scopes(base / "trailing", False, scopes)
    assert matches_scopes(base / "a/b", False, scopes)
    assert matches_scopes(base / "a/x/y/b", False, scopes)
    assert matches_scopes(base / "cache/item.bin", False, scopes)
    assert not matches_scopes(base / "cache", True, scopes)
    assert matches_scopes(base / "x.o", False, scopes)
    assert matches_scopes(base / "x.a", False, scopes)
    assert not matches_scopes(base / "z.o", False, scopes)
