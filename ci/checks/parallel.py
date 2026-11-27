"""CLI acceptance: partition loss/duplicates, swallowed failures, collection errors, diagnostics."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "compare_tests.py"


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        serial_file = "tests/harness/test_diagnostic_history.py"
        for name in ("tests/ai/test_auth.py", "tests/ai/test_models.py", serial_file):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_" + path.stem + "(): assert True\n")

        def run(mode, expected, *, suite="all", paths=()):
            output = root / mode
            result = subprocess.run([
                sys.executable, str(RUNNER), "run", "--project", str(root),
                "--output", str(output), "--platform", "local", "--sha", "test",
                "--execution", mode, "--suite", suite, *paths,
            ], capture_output=True, text=True)
            assert result.returncode == expected, result.stdout + result.stderr
            return json.loads((output / "results.json").read_text())

        before = run("serial", 0)
        after = run("parallel", 0)
        assert before["tests"] == after["tests"] and len(after["tests"]) == 3
        assert (root / "parallel" / "serial.xml").exists(), "manifest serial file must run in serial shard"
        assert (root / "parallel" / "parallel-0.xml").exists()
        (root / "tests/ai/test_auth.py").write_text('def test_failure():\n    assert False, "diagnostic sentinel"\n')
        assert run("parallel", 1)["counts"]["failed"] == 1
        details = (root / "parallel/failures.md").read_text()
        assert "diagnostic sentinel" in details and "test_auth.py" in details and "test_failure" in details
        (root / serial_file).write_text('raise RuntimeError("collection sentinel")\n')
        assert not run("parallel", 2)["complete"]

        # CI partitions the same checkout into disjoint common/native jobs. The
        # native job must actually execute, force capabilities, and reject skips.
        (root / serial_file).write_text("def test_serial(): pass\n")
        (root / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
            'addopts = ["--strict-markers"]\nmarkers = ["native: native acceptance"]\n'
        )
        native = root / "tests/harness/test_sandbox_e2e.py"
        native.write_text(
            'import os\nimport pytest\n'
            'def test_broker(): pass\n'
            '@pytest.mark.native\ndef test_native():\n'
            '    assert os.environ["VIRGO_REQUIRE_SANDBOX_TESTS"] == "1"\n'
        )
        (root / "tests/ai/test_auth.py").write_text("def test_auth(): pass\n")
        common = run("parallel", 0, suite="common")
        isolated = run("serial", 0, suite="native", paths=("tests/harness/test_sandbox_e2e.py",))
        assert common["suite"] == "common" and isolated["suite"] == "native"
        assert len(common["tests"]) == 4 and len(isolated["tests"]) == 1
        assert not common["tests"].keys() & isolated["tests"].keys()
        assert any(name.endswith("::test_broker") for name in common["tests"])
        native.write_text(
            'import pytest\n@pytest.mark.native\n@pytest.mark.skip(reason="missing backend")\n'
            'def test_native(): pass\n'
        )
        assert run("serial", 1, suite="native", paths=("tests/harness/test_sandbox_e2e.py",))["problems"]
        native.write_text("def test_broker(): pass\n")
        assert run("serial", 5, suite="native", paths=("tests/harness/test_sandbox_e2e.py",))["problems"]
    print("Parallel CLI acceptance passed")


if __name__ == '__main__':
    main()
