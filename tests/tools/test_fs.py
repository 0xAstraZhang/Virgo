"""File tools: read paging, write, edit matching, glob and grep."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from virgo.agent.types import ToolContext
from virgo.execution import SandboxExecutor
from virgo.tools import EDIT, GLOB, GREP, READ, WRITE
from virgo.tools.fs import _prepare_edit, make_file_tool
from virgo.tools.output import MAX_LINES

from tests.support.execution import file_policy


def ctx(cwd: Path) -> ToolContext:
    return ToolContext("c1", cwd, threading.Event())


@pytest.fixture
def file_executor():
    executor = SandboxExecutor()
    yield executor
    executor.close()


def edit(cwd, path, *pairs):
    args = _prepare_edit({"path": path, "edits": [{"old_text": o, "new_text": n} for o, n in pairs]})
    return EDIT.execute(args, ctx(cwd))


def test_read_pages_large_files(project):
    f = project / "big.txt"
    f.write_text("".join(f"line {i}\n" for i in range(1, MAX_LINES + 501)), newline="\n")
    r = READ.execute({"path": "big.txt"}, ctx(project))
    assert f"Use offset={MAX_LINES + 1}" in r.as_text()
    r2 = READ.execute({"path": "big.txt", "offset": MAX_LINES + 1, "limit": 3}, ctx(project))
    assert r2.as_text().startswith(f"line {MAX_LINES + 1}\n")
    assert READ.execute({"path": "missing.txt"}, ctx(project)).is_error


def test_worker_snapshot_survives_edits_to_its_installation(project, monkeypatch):
    import shutil

    from virgo import file_operations
    from virgo.execution import HostContext

    installation = project / "editable-install"
    installation.mkdir()
    source = Path(file_operations.__file__).parent
    for name in ("fs_worker.py", "fs_windows.py"):
        shutil.copyfile(source / name, installation / name)
    monkeypatch.setattr(file_operations, "__file__", str(installation / "file_operations.py"))
    executor = SandboxExecutor()
    try:
        host = HostContext(project, executor=executor)
        # Represent editing Virgo itself after session initialization. The next
        # actual worker must execute its captured snapshot, never these bytes.
        (installation / "fs_worker.py").write_text("raise SystemExit(73)\n")
        response = host.run_file_operation("write", {"path": "captured.txt", "content": "trusted snapshot"})
        assert response["ok"] and response["boundary"]["payload_started"] is True, response
        assert (project / "captured.txt").read_text() == "trusted snapshot"
        # A later executor in the same Broker must not recapture a checkout
        # that an earlier model operation has already changed.
        later = SandboxExecutor()
        try:
            response = HostContext(project, executor=later).run_file_operation("read", {"path": "captured.txt"})
            assert response["ok"] and response["boundary"]["payload_started"] is True, response
        finally:
            later.close()
    finally:
        executor.close()


@pytest.mark.parametrize("tool,arguments", [
    (READ, {"path": "secret.txt"}),
    (GREP, {"path": ".", "pattern": "private"}),
    (GLOB, {"path": ".", "pattern": "*"}),
])
def test_file_tools_enforce_read_scope(project, tool, arguments, file_executor):
    outside = project.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private-value")
    arguments = {**arguments, "path": str(outside / arguments["path"])}
    policy = file_policy(project, executor=file_executor)
    wrapped = make_file_tool(tool, lambda _ctx: policy)
    denied = wrapped.execute(arguments, ctx(project))
    assert denied.is_error, denied.as_text()
    assert denied.details["boundary"]["state"] == "established", denied.as_text()
    assert "private-value" not in denied.as_text()
    full_read = file_policy(project, read_paths=(project, outside), executor=file_executor)
    allowed = make_file_tool(tool, lambda _ctx: full_read).execute(arguments, ctx(project))
    assert not allowed.is_error, allowed.as_text()


@pytest.mark.parametrize("tool", [READ, GREP])
def test_scoped_read_cannot_follow_external_symlink(project, tool, file_executor):
    outside = project.parent / "secret.txt"
    outside.write_text("private-value")
    policy = file_policy(project, executor=file_executor)
    (project / "ordinary.txt").write_text("visible-value")
    positive = make_file_tool(tool, lambda _ctx: policy).execute(
        {"path": "ordinary.txt", **({"pattern": "visible"} if tool is GREP else {})}, ctx(project))
    assert not positive.is_error and positive.details["boundary"]["state"] == "established", positive.as_text()
    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating file symlinks is unavailable")
    arguments = {"path": "link.txt", **({"pattern": "private"} if tool is GREP else {})}
    result = make_file_tool(tool, lambda _ctx: policy).execute(arguments, ctx(project))
    assert result.is_error and "private-value" not in result.as_text()


def test_file_tools_keep_private_storage_hidden_and_full_scope_writes_explicit(project, file_executor):
    private = project.parent / "home"
    secret = private / "secret.txt"
    secret.write_text("private-value")
    outside = project.parent / "extra"
    outside.mkdir()
    policy = file_policy(project, read_paths=(project, outside), write_paths=(project, outside),
                         private_paths=(private,), executor=file_executor)
    read = make_file_tool(READ, lambda _ctx: policy)
    denied = read.execute({"path": str(secret)}, ctx(project))
    assert denied.is_error and "private-value" not in denied.as_text()
    target = outside / "outside.txt"
    write = make_file_tool(WRITE, lambda _ctx: policy)
    result = write.execute({"path": str(target), "content": "allowed"}, ctx(project))
    assert not result.is_error and target.read_text() == "allowed", result.as_text()
    readonly = file_policy(project, read_paths=(project, outside), executor=file_executor)
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


def test_fs_worker_tilde_double_slash_and_symlink_loop_and_ebusy(project, monkeypatch):
    import errno
    import os
    from concurrent.futures import ThreadPoolExecutor

    from virgo import fs_worker
    from virgo.file_operations import FileOperations

    home = project / "fake-home"
    home.mkdir()
    assert fs_worker.resolve("~//nested/file.txt", project, str(home)) == (home / "nested" / "file.txt").resolve()

    if os.name == "posix":
        loop_a, loop_b = project / "loop-a", project / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        res = READ.execute({"path": str(loop_a)}, ctx(project))
        assert res.is_error and res.details["status"] == "file_operation_failed"

        target = project / "protected.txt"
        target.write_text("original")

        def busy(*_args, **_kwargs):
            raise OSError(errno.EBUSY, "Device or resource busy")

        monkeypatch.setattr(os, "replace", busy)
        with pytest.raises(PermissionError):
            with fs_worker._mutation(target, create=True) as (stream, _, commit):
                stream.write(b"changed")
                commit()
        assert target.read_text() == "original"

    files = FileOperations()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(files.close) for _ in range(4)]
        for future in futures:
            future.result()
    assert not files.directory.exists()


def test_read_output_handles_none_and_invalid_bounds(make_session, project):
    from virgo.agent import ToolContext

    session = make_session()
    tool = session.tool("read_output")
    record, stream = session.main.execution_context.outputs.create("test")
    with stream:
        stream.write(b"abcdef")
    context = ToolContext("read-out", project, threading.Event(), origin=session.main.origin)
    result = tool.execute({"output_id": record.id, "offset": None, "limit": None}, context)
    assert not result.is_error and result.as_text() == "abcdef"
    invalid = tool.execute({"output_id": record.id, "offset": "bad"}, context)
    assert invalid.is_error
    first_store = session.main.execution_context.outputs
    session.new_session()
    assert session.main.execution_context.outputs is not first_store
