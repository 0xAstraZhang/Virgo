"""Exercise the comparison CLI using real pytest runs in temporary projects."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).with_name("compare_tests.py")


def main():
    with tempfile.TemporaryDirectory(prefix="virgo-ci-check-") as directory:
        root = Path(directory)

        def run(label, source, *, extra=None):
            project = root / label
            project.mkdir(exist_ok=True)
            (project / "test_cases.py").write_text(source, encoding="utf-8")
            if extra:
                (project / "test_other.py").write_text(extra, encoding="utf-8")
            subprocess.run([
                sys.executable, str(RUNNER), "run", "--project", str(project),
                "--output", str(root / "reports" / label), "--platform", "local",
                "--label", label, "--sha", label,
            ], check=False, stdout=subprocess.DEVNULL)

        def compare(expected):
            result = subprocess.run([
                sys.executable, str(RUNNER), "compare", "--reports", str(root / "reports"),
                "--output", str(root / "comparison"), "--platforms", "local",
            ], check=False, stdout=subprocess.DEVNULL)
            assert result.returncode == expected, (root / "comparison.md").read_text()
            return json.loads((root / "comparison.json").read_text())["platforms"]["local"]

        common = "import pytest\ndef test_same(): assert False\n"
        run("base", common + "def test_fixed(): assert False\ndef test_moved(): pass\n")
        run("head", common + "def test_fixed(): pass\n", extra="def test_moved(): pass\n")
        report = compare(0)
        assert report["existing_failures"] == ["test_same"]
        assert report["fixed_failures"] == ["test_fixed"]
        assert report["added"] == report["removed"] == []

        run("base", common + "def test_new(): pass\n@pytest.mark.skip\ndef test_exposed(): pass\n")
        run("head", common + "def test_new(): assert False\ndef test_exposed(): assert False\n"
            "def test_added_failure(): assert False\n")
        report = compare(1)
        assert report["regressions"] == ["test_new"]
        assert report["newly_exposed_failures"] == ["test_added_failure", "test_exposed"]

        run("base", "def test_removed(): pass\ndef test_skipped(): pass\n")
        run("head", "import pytest\n@pytest.mark.skip\ndef test_skipped(): pass\n")
        report = compare(1)
        assert report["removed"] == ["test_removed"]
        assert report["new_skips"] == ["test_skipped"]

        for label, parameter in [("base", "virgo.ai.providers.responses"), ("head", "openai-responses")]:
            (root / label / "test_other.py").unlink(missing_ok=True)
            run(label, "import pytest\n" + f"@pytest.mark.parametrize('api', [{parameter!r}])\n"
                "def test_compaction_does_not_enable_native_search(api): pass\n")
        report = compare(0)
        assert not report["added"] and not report["removed"]

        run("head", "def test_duplicate(): pass\n", extra="def test_duplicate(): pass\n")
        assert compare(1)["problems"]
        (root / "head" / "test_other.py").unlink()
        run("head", "raise RuntimeError('collection failed')\n")
        assert compare(1)["problems"]
        (root / "reports" / "head" / "results.json").unlink()
        assert compare(1)["problems"]
    print("CLI end-to-end checks passed")


if __name__ == "__main__":
    main()
