"""Historical diagnostics remain accessible without widening model authority."""

import json

from virgo.diagnostics import capture_error
from virgo.harness.diagnostics import DiagnosticLog


def test_old_errors_survive_history_growth_and_restart(make_session):
    session = make_session(persist=True, refresh_models=False)
    first = session.main.report_error("oldest retained failure", phase="fixture.history", notify=False)
    for number in range(1100):
        session.main.report_error(f"later failure {number}", phase="fixture.history", notify=False)
    assert session.main.diagnostic_access.read(first["error_id"])["status"] == "ok"
    session.prompt(f"/diagnostics {first['error_id']}")
    assert "oldest retained failure" in session.ui.text

    restored = make_session(session=session.store.path, persist=True, refresh_models=False)
    assert restored.main.diagnostic_access.read(first["error_id"])["status"] == "ok"
    stranger = make_session(refresh_models=False)
    stranger.prompt(f"/diagnostics {first['error_id']}")
    assert "oldest retained failure" in stranger.ui.text
    assert stranger.main.diagnostic_access.read(first["error_id"])["status"] == "not_found"


def test_legacy_history_import_is_incremental_and_does_not_grant_model_access(make_session, tmp_path):
    session = make_session(refresh_models=False)
    directory = session.diagnostics.path.parent
    directory.mkdir(parents=True, exist_ok=True)
    first = capture_error("legacy failure", phase="fixture.legacy")
    second = capture_error("unfinished legacy failure", phase="fixture.legacy")
    path = directory / "harness-20260101T000000-42-fixture.jsonl.1"
    original = json.dumps(first) + "\ninvalid JSON\n" + json.dumps(second)
    path.write_text(original)
    session.prompt(f"/diagnostics {first['error_id']}")
    assert "legacy failure" in session.ui.text
    assert session.main.diagnostic_access.read(first["error_id"])["status"] == "not_found"
    path.write_text(original + "\n")
    session.prompt(f"/diagnostics {second['error_id']}")
    assert "unfinished legacy failure" in session.ui.text
    assert path.read_text() == original + "\n"
    session.prompt(f"/diagnostics {first['error_id']}")
    with DiagnosticLog(directory) as log:
        assert len(log.history_page()["records"]) == 2


def test_global_history_paging_and_literal_prefixes_do_not_change_model_context(make_session):
    session = make_session(refresh_models=False)
    saved = []
    for number in range(27):
        record = capture_error(f"history {number}", phase="fixture.history")
        record["error_id"] = f"literal_{number:02d}"
        saved.append(session.diagnostics.record(record))
    session.diagnostics.record({**saved[0], "error_id": "literalX00"})
    restarted = make_session(refresh_models=False)
    assert restarted.diagnostics.lookup("literal_00") == saved[0]
    assert restarted.diagnostics.lookup("literal_") is None
    assert restarted.diagnostics.lookup("literal_0") is None
    ids, before = [], None
    while True:
        page = restarted.diagnostics.history_page(before)
        ids.extend(record["error_id"] for record in page["records"])
        assert len(page["records"]) <= 10
        before = page["next_cursor"]
        if before is None:
            break
        restarted.prompt(f"/diagnostics --before {before}")
    assert len(ids) == len(set(ids)) == 28
    assert restarted.main.messages == []
    restarted.prompt("/diagnostics --before invalid")
    assert "Invalid history cursor" in restarted.ui.text


def test_corrupt_durable_database_does_not_prevent_startup_or_replace_errors(make_session, tmp_path, capsys):
    import os

    # The same protected user directory is used by the real session constructor.
    from pathlib import Path

    path = Path(os.environ["VIRGO_HOME"]) / "logs" / "diagnostics.sqlite3"
    path.parent.mkdir()
    path.write_bytes(b"not a SQLite database")
    session = make_session(refresh_models=False)
    original = session.main.report_error("original operation failed", phase="fixture.corrupt", notify=False)
    assert original["message"] == "original operation failed"
    assert "logging_error" in original and "log_path" not in original
    assert "original operation failed" in capsys.readouterr().err
    # Runtime snapshots remain independent of a broken global log.
    assert session.main.diagnostic_access.read(original["error_id"])["status"] == "ok"
    session.prompt("/diagnostics")
    assert "could not be read" in session.ui.text


def test_concurrent_processes_commit_unique_errors_to_the_same_database(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from tests.support.persistence import diagnostic_records

    code = '''
import sys
from pathlib import Path
from virgo.diagnostics import capture_error
from virgo.harness.diagnostics import DiagnosticLog
with DiagnosticLog(Path(sys.argv[1])) as log:
    log.record({"error_id": "shared", "message": "shared error", "phase": "fixture.parallel"})
    for number in range(40):
        saved = log.record(capture_error(f"worker error {number}", phase="fixture.parallel"))
        assert "logging_error" not in saved
'''
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                                 cwd=Path(__file__).resolve().parents[2],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stdout + stderr
    assert len(diagnostic_records(tmp_path / "diagnostics.sqlite3")) == 161


def test_busy_database_preserves_original_error_and_recovers_on_rereport(tmp_path, capsys):
    import sqlite3
    import time
    from contextlib import closing

    with DiagnosticLog(tmp_path) as log:
        log.record(capture_error("initial", phase="fixture.lock"))
        original = capture_error("original locked operation", phase="fixture.lock")
        with closing(sqlite3.connect(log.path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            unsaved = log.record(original)
            assert time.monotonic() - start < 3
            assert "locked" in unsaved["logging_error"]
            assert unsaved["error_id"] == original["error_id"] and "log_path" not in unsaved
            assert "original locked operation" in capsys.readouterr().err
            blocker.rollback()
        saved = log.record(original)
        assert "logging_error" not in saved and saved["log_path"] == str(log.path)
        assert log.lookup(original["error_id"]) == saved
