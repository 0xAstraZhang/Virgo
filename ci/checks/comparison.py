"""Exercise the summary CLI using real pytest runs in temporary projects."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "compare_tests.py"


def main():
    with tempfile.TemporaryDirectory(prefix="virgo-ci-check-") as directory:
        root = Path(directory)
        project = root / "head"
        project.mkdir()

        def run(source, *, extra=None, extra_name="test_other.py"):
            (project / "test_cases.py").write_text(source, encoding="utf-8")
            for name in ("test_other.py", "other_test.py"):
                (project / name).unlink(missing_ok=True)
            if extra is not None:
                (project / extra_name).write_text(extra, encoding="utf-8")
            subprocess.run([
                sys.executable, str(RUNNER), "run", "--project", str(project),
                "--output", str(root / "reports" / "local"), "--platform", "local",
                "--sha", "head",
            ], check=False, stdout=subprocess.DEVNULL)

        def summarize(expected, *options, reports_dir=None):
            for suffix in (".md", ".json"):
                (root / "summary").with_suffix(suffix).unlink(missing_ok=True)
            result = subprocess.run([
                sys.executable, str(RUNNER), "summarize",
                "--reports", str(reports_dir or (root / "reports")),
                "--output", str(root / "summary"), "--platforms", "local", *options,
            ], check=False, stdout=subprocess.DEVNULL)
            assert (root / "summary.md").exists(), "Missing readable failure summary"
            assert result.returncode == expected, (root / "summary.md").read_text()
            return json.loads((root / "summary.json").read_text())["platforms"].get("local", {})

        # Passing suite succeeds cleanly.
        run("def test_ok(): pass\n")
        report = summarize(0)
        assert not report["failures"] and not report["problems"]
        assert report["counts"] == {"passed": 1}

        # Failing test fails the gate and is reported by classname::name.
        run("def test_ok(): pass\ndef test_failure(): assert False\n")
        report = summarize(1)
        assert report["failures"] == ["test_cases::test_failure"]

        # Platform/conditional skip passes cleanly.
        run("import pytest\n@pytest.mark.skip(reason='platform')\ndef test_skip(): pass\n")
        report = summarize(0)
        assert not report["failures"] and report["counts"] == {"skipped": 1}

        # Identical function names in separate test modules remain distinct and valid.
        run("def test_shared(): pass\n", extra="def test_shared(): pass\n")
        report = summarize(0)
        assert not report["problems"] and report["counts"] == {"passed": 2}

        # A module-level skip remains a separate outcome, not a missing case.
        run(
            "def test_ok(): pass\n",
            extra="import pytest\npytest.skip('module', allow_module_level=True)\n",
            extra_name="other_test.py",
        )
        report = summarize(0)
        assert report["collection_skips"] == ["other_test"]
        assert report["counts"] == {"passed": 1, "skipped": 1}

        # Collection failure and missing reports both fail the gate.
        run("raise RuntimeError('collection failed')\n")
        assert summarize(1)["problems"]
        report_path = root / "reports" / "local" / "results.json"
        report_path.unlink()
        assert summarize(1)["problems"]

        # Report integrity, target matching, upstream job results, and docs-only mode.
        run("def test_ok(): pass\n")
        valid = json.loads(report_path.read_text())
        target_path = root / "target.json"
        target = {"pr": 10, "url": "https://github.com/owner/project/pull/10",
                  "base_sha": "base", "head_sha": "head", "run_tests": True}
        target_path.write_text(json.dumps(target))
        summarize(0, "--target", str(target_path))

        # Native jobs are independent required reports, not optional extras to a
        # passing common suite. The gate rejects missing, skipped, wrong-SHA, or
        # ordinary reports presented as native acceptance.
        native_dir = root / "reports" / "native"
        native_dir.mkdir()
        native_path = native_dir / "results.json"
        common = {**valid, "suite": "common"}
        native = {**valid, "platform": "native-local", "suite": "native"}
        report_path.write_text(json.dumps(common))
        native_options = ("--target", str(target_path), "--native-platforms", "native-local",
                          "--native-result", "success")
        native_path.write_text(json.dumps(native))
        summarize(0, *native_options)
        for bad_native in (
            {**native, "suite": "common"},
            {**native, "sha": "other-commit"},
            {**native, "counts": {"skipped": 1},
             "tests": {"test_cases::test_ok": {"status": "skipped"}}},
            {**native, "counts": {}, "tests": {}},
        ):
            native_path.write_text(json.dumps(bad_native))
            summarize(1, *native_options)
        native_path.unlink()
        summarize(1, *native_options)
        native_path.write_text(json.dumps(native))
        report_path.write_text(json.dumps(valid))
        summarize(1, *native_options)
        native_path.unlink()

        # A partial rerun replaces only the retried platform, including a later failure
        # or an incomplete upload. Summary-only retries re-use the same selection.
        attempts = root / "attempts"
        linux = attempts / "results-local-attempt-2"
        other = attempts / "results-other-attempt-1"
        latest = attempts / "results-local-attempt-10"
        for directory in (linux, other, latest):
            directory.mkdir(parents=True)
        failing = {**valid, "counts": {"failed": 1}, "exit_code": 1,
                   "tests": {"test_cases::test_ok": {"status": "failed"}}}
        (linux / "results.json").write_text(json.dumps(failing))
        (other / "results.json").write_text(json.dumps({**valid, "platform": "other"}))
        current = latest / "results.json"
        options = ("--target", str(target_path), "--platforms", "local", "other", "--run-attempt", "11")
        current.write_text(json.dumps(valid))
        assert summarize(0, *options, reports_dir=attempts)["counts"] == {"passed": 1}
        (linux / "results.json").write_text(json.dumps(valid))
        current.write_text(json.dumps(failing))
        assert summarize(1, *options, reports_dir=attempts)["failures"] == ["test_cases::test_ok"]
        current.unlink()
        assert summarize(1, *options, reports_dir=attempts)["problems"]
        current.write_text(json.dumps({**valid, "platform": "other"}))
        summarize(1, *options, reports_dir=attempts)

        bad_reports = [
            {**valid, "complete": False, "exit_code": 2, "problems": []},
            {**valid, "complete": False},
            {**valid, "exit_code": 2},
            {**valid, "exit_code": 1},
            {**valid, "exit_code": True},
            {**valid, "tests": {}, "counts": {}},
            {**valid, "counts": {"passed": 99}},
            {**valid, "counts": {"passed": True}},
            {**valid, "tests": {"test_ok": {"status": "unknown"}}},
            {**valid, "sha": "wrong"},
            {**valid, "platform": "unexpected"},
            {**valid, "duration": -1},
            {**valid, "collection_skips": "wrong"},
            {**valid, "problems": "wrong"},
            {}, [],
        ]
        for bad in bad_reports:
            report_path.write_text(json.dumps(bad))
            summarize(1, "--target", str(target_path))
        for invalid in ('{', '{"platform":"local","platform":"other"}'):
            report_path.write_text(invalid)
            summarize(1, "--target", str(target_path))

        report_path.write_text(json.dumps(valid))
        duplicate = root / "reports" / "duplicate" / "results.json"
        duplicate.parent.mkdir()
        duplicate.write_text(json.dumps(valid))
        summarize(1, "--target", str(target_path))
        duplicate.unlink()

        for invalid in ('{', '{}', '[]'):
            target_path.write_text(invalid)
            summarize(1, "--target", str(target_path))
        target_path.unlink()
        summarize(1, "--target", str(target_path))
        for job in ("resolve", "checks", "test", "native"):
            for status in ("failure", "cancelled", "skipped"):
                summarize(1, f"--{job}-result", status)

        # Documentation skips require a valid target, successful checks, and no unexpected reports.
        target.update(run_tests=False, skip_reason="Only documentation changed")
        target_path.write_text(json.dumps(target))
        # An unexpected test report when run_tests=False fails the gate.
        summarize(1, "--target", str(target_path), "--resolve-result", "success",
                  "--checks-result", "success", "--test-result", "skipped")
        report_path.unlink()
        # Works even when the reports directory was never created (download skipped).
        summarize(0, "--target", str(target_path), "--resolve-result", "success",
                  "--checks-result", "success", "--test-result", "skipped", "--native-result", "skipped",
                  "--native-platforms", "native-local",
                  reports_dir=root / "missing-reports")
        summary = json.loads((root / "summary.json").read_text())
        assert summary["skipped"] and not summary["needs_attention"]
        assert "Only documentation changed" in (root / "summary.md").read_text()
        summarize(1, "--target", str(target_path), "--checks-result", "failure", "--test-result", "skipped")
        target["run_tests"] = "false"
        target_path.write_text(json.dumps(target))
        summarize(1, "--target", str(target_path))
    print("CLI end-to-end checks passed")


if __name__ == "__main__":
    main()
