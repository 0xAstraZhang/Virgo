from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from virgo.agent.types import ToolContext
from virgo.ai.validate import INVALID_ARGUMENTS, parse_json_loose, parse_tool_arguments, validate
from virgo.tools import BASH, EDIT, GLOB, GREP, READ, WRITE
from virgo.tools.fs import _prepare_edit
from virgo.tools.truncate import MAX_LINES


def ctx(cwd: Path) -> ToolContext:
    return ToolContext("c1", cwd, threading.Event())


def edit(cwd, path, *pairs):
    args = _prepare_edit({"path": path, "edits": [{"old_text": o, "new_text": n} for o, n in pairs]})
    return EDIT.execute(args, ctx(cwd))


def test_read_pages_large_files(project):
    f = project / "big.txt"
    f.write_text("".join(f"line {i}\n" for i in range(1, MAX_LINES + 501)))
    r = READ.execute({"path": "big.txt"}, ctx(project))
    assert f"Use offset={MAX_LINES + 1}" in r.as_text()
    r2 = READ.execute({"path": "big.txt", "offset": MAX_LINES + 1, "limit": 3}, ctx(project))
    assert r2.as_text().startswith(f"line {MAX_LINES + 1}\n")
    assert READ.execute({"path": "missing.txt"}, ctx(project)).is_error


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
    assert "src/a.py" in g and "skip.py" not in g
    out = GREP.execute({"pattern": "hello", "glob": "*.py"}, ctx(project)).as_text()
    assert out == "src/a.py:1:def hello():"
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
        return set() if out == "(no matches)" else set(out.splitlines())

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
        return {line.split(":", 1)[0] for line in out.splitlines()}

    assert searched("**/*.py") == nested | {"main.py", "docs/a.py"}  # root-level files were skipped
    assert searched("src/**/*.py") == nested
    assert searched("src/*.py") == {"src/top.py"}
    assert searched("*.md") == {"src/pkg/notes.md"}


def test_bash_exit_codes_timeout_and_truncation(project):
    r = BASH.execute({"command": "echo hi; exit 3"}, ctx(project))
    assert r.is_error and "hi" in r.as_text() and "Exit code 3" in r.as_text()
    assert BASH.execute({"command": "pwd"}, ctx(project)).as_text().strip() == str(project)
    t = BASH.execute({"command": "sleep 5", "timeout": 0.3}, ctx(project))
    assert t.is_error and "timed out" in t.as_text()
    big = BASH.execute({"command": f"seq 1 {MAX_LINES + 100}"}, ctx(project)).as_text()
    assert "Output truncated" in big and big.rstrip().endswith(str(MAX_LINES + 100))
    saved = Path(big.split("Full output: ")[1].split("]")[0])
    assert saved.parent == project / ".virgo" / "outputs"
    assert saved.read_text().startswith("1\n")


def test_bash_marks_child_processes(project):
    result = BASH.execute({"command": 'printf "%s" "$VIRGO"'}, ctx(project))
    assert not result.is_error and result.as_text() == "1"


def test_shell_environment_override(project):
    code = (
        "from pathlib import Path; from virgo.tools.bash import spawn; "
        "proc = spawn(\"import os; print(os.environ['VIRGO'])\", Path.cwd()); "
        "out, _ = proc.communicate(timeout=5); print(out.decode(), end=''); "
        "raise SystemExit(proc.returncode)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project,
        env={**os.environ, "VIRGO_SHELL": sys.executable},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "1"


def test_validate_coercions():
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "flag": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "string"}},
            "obj": {"type": "object", "properties": {"k": {"type": "string"}}, "required": ["k"]},
        },
        "required": ["n"],
    }
    v, errs = validate({"n": "4", "flag": "true", "items": "one", "obj": '{"k": "v"}'}, schema)
    assert errs == [] and v == {"n": 4, "flag": True, "items": ["one"], "obj": {"k": "v"}}
    _, errs = validate({"obj": {}}, schema)
    assert any("n: required" in e for e in errs) and any("k: required" in e for e in errs)


def test_tool_arguments_must_be_a_json_object():
    assert parse_tool_arguments('{"path": "a"}') == {"path": "a"}
    assert parse_tool_arguments("") == parse_tool_arguments("  ") == {}
    for raw in ("null", '"src"', "[1, 2]", "3", '{"path": '):
        assert parse_tool_arguments(raw) == {INVALID_ARGUMENTS: raw}


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Sure! {"ok": true} hope that helps') == {"ok": True}
