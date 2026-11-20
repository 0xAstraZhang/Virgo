"""Tests for session-persisted Broker OutputStore, piped shell capture, and read_output (S3-09 / VER-08)."""
from __future__ import annotations

import os
import time

from virgo.ai.providers import faux
from virgo.output_store import OutputStore
from virgo.tools.output import MAX_BYTES, save_output

from tests.support.harness import tool_results
from tests.support.sandbox import python_command, run


def test_ver08_read_output_survives_session_resume(make_session, project):
    """VER-08: Output spilled in a persisted session remains readable after session resume."""
    payload = "line-content-" + ("你好世界🚀" * 10000)
    first = make_session(session="new", extensions=False, refresh_models=False)
    try:
        record = save_output(payload, project, "shell", execution_context=first.main.execution_context)
        output_id = str(record)
        session_path = first.store.path
        assert session_path is not None
        # Ensure the session file is non-empty so shutdown retains it.
        faux.script("test", faux.reply("saved"))
        first.prompt("keep session")
    finally:
        first.shutdown()

    resumed = make_session(session=session_path, extensions=False, refresh_models=False)
    try:
        collected = []
        offset = 0
        while True:
            faux.script(
                "test",
                faux.reply(tools=[("read_output", {"output_id": output_id, "offset": offset, "limit": 4096})]),
                faux.reply("done"),
            )
            resumed.prompt("read output page")
            res = tool_results(resumed)[-1]
            assert not res.is_error, res.text()
            text = res.text()
            assert "\ufffd" not in text, "UTF-8 boundary alignment must not split multibyte characters"
            collected.append(text)
            next_offset = res.details["next_offset"]
            if next_offset is None:
                break
            offset = next_offset
        assert "".join(collected) == payload
    finally:
        resumed.shutdown()


def test_s309_untruncated_command_leaves_no_log_files(make_session, project):
    """S3-09: Normal un-truncated foreground commands must not leave leftover .log files."""
    session = make_session(
        session="new", settings={"execution": {"mode": "no-limit"}},
        extensions=False, refresh_models=False,
    )
    try:
        store = session.main.execution_context.outputs
        result = run(session, python_command(session, "print('hello-short-output')"))
        assert not result.is_error and "hello-short-output" in result.text()
        assert result.details["output_id"] is None
        logs = list(store.directory.glob("*.log")) if store.directory.exists() else []
        assert logs == [], f"Unexpected leftover log files: {logs}"
    finally:
        session.shutdown()


def test_s309_truncated_command_persists_record_and_trash_cleans_up(make_session, project, trash):
    """S3-09: Truncated command persists an output_id; trashing the session deletes its outputs."""
    session = make_session(
        session="new", settings={"execution": {"mode": "no-limit"}},
        extensions=False, refresh_models=False,
    )
    try:
        session_path = session.store.path
        assert session_path is not None
        out_dir = session.main.execution_context.outputs.directory
        result = run(session, python_command(session, f"print('x' * {MAX_BYTES + 4096})"))
        assert not result.is_error and result.details["truncated"] is True
        output_id = result.details["output_id"]
        assert output_id and output_id.startswith("out_")
        assert list(out_dir.glob("*.log"))

        faux.script(
            "test",
            faux.reply(tools=[("read_output", {"output_id": output_id, "offset": 0, "limit": 100})]),
            faux.reply("done"),
        )
        session.prompt("inspect saved output")
        page = tool_results(session)[-1]
        assert not page.is_error and page.text() == "x" * 100

        session.trash_session(session_path)
        assert not out_dir.exists()
    finally:
        session.shutdown()


def test_s309_utf8_pagination_and_ttl_and_cross_session_isolation(tmp_path):
    """S3-09: UTF-8 boundary alignment, TTL pruning, and cross-session output isolation."""
    outputs_root = tmp_path / "outputs"
    store_a = OutputStore(outputs_root / "session-a")
    store_b = OutputStore(outputs_root / "session-b")

    sample = "中文测试🎉🚀αβγ" * 50
    rec, pipe = store_a.create("test")
    with pipe:
        pipe.write(sample.encode("utf-8"))

    # Read with small odd byte limit (7 bytes) to force cuts inside 3-byte and 4-byte UTF-8 chars.
    chunks = []
    offset = 0
    while True:
        data, total, start = store_a.read(rec, offset=offset, limit=7, align_utf8=True)
        decoded = data.decode("utf-8")  # Must decode strictly without errors!
        chunks.append(decoded)
        end = start + len(data)
        if end >= total:
            break
        offset = end
    assert "".join(chunks) == sample

    # Cross-session isolation.
    from virgo.execution import FileAccessDenied
    try:
        store_b.lookup(rec.id)
    except FileAccessDenied:
        pass
    else:
        raise AssertionError("Session B must not look up Session A's output record")

    # TTL pruning removes old session dirs while preserving recent ones.
    old_dir = outputs_root / "session-expired"
    old_dir.mkdir(parents=True)
    (old_dir / "old.log").write_text("stale")
    old_time = time.time() - 3600 * 48
    os.utime(old_dir, (old_time, old_time))
    removed = OutputStore.prune_expired(outputs_root, max_age_seconds=3600)
    assert removed == 1
    assert not old_dir.exists()
    assert (outputs_root / "session-a").exists()
