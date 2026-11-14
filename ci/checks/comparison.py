"""Exercise the comparison CLI using real pytest runs in temporary projects."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "compare_tests.py"


def check_retirements(root):
    """Exercise the reviewed deletion through real pytest runs and comparison CLI."""
    baseline = "0ffc23707de6e87a579a46a8f027679c253b78dc"
    retired = "test_result_hook_failure_preserves_the_original_tool_failure"
    retained = "test_projection_sees_final_hooks_and_recorded_evidence_once_in_provider_content"

    def run(label, *, old=None, coverage="pass", sha=None, module="tests/agent/test_diagnostics.py",
            unrelated=False):
        project = root / label
        shutil.rmtree(project, ignore_errors=True)
        project.mkdir(parents=True)
        (project / "test_smoke.py").write_text("def test_smoke(): pass\n", encoding="utf-8")
        for path, name, body in ((module, retired, old),
                                 ("tests/agent/test_diagnostic_projection.py", retained, coverage)):
            if body is not None:
                target = project / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"import pytest\ndef {name}(): {body}\n", encoding="utf-8")
        if unrelated:
            (project / "test_other.py").write_text("def test_unapproved_removal(): pass\n", encoding="utf-8")
        subprocess.run([
            sys.executable, str(RUNNER), "run", "--project", str(project),
            "--output", str(root / "reports" / label), "--platform", "local",
            "--label", label, "--sha", sha or (baseline if label == "base" else "head"),
        ], check=False, stdout=subprocess.DEVNULL)

    def compare(expected):
        for suffix in (".md", ".json"):
            (root / "comparison").with_suffix(suffix).unlink(missing_ok=True)
        result = subprocess.run([
            sys.executable, str(RUNNER), "compare", "--reports", str(root / "reports"),
            "--output", str(root / "comparison"), "--platforms", "local",
        ], check=False, stdout=subprocess.DEVNULL)
        text = (root / "comparison.md").read_text(encoding="utf-8")
        assert result.returncode == expected, text
        report = json.loads((root / "comparison.json").read_text(encoding="utf-8"))["platforms"]["local"]
        assert retired in text, "The deletion must stay visible in the readable report"
        return report

    # Failure modes are recorded in docs/en/development/testing-and-ci.md before the comparison change.
    run("base", old="pass")
    run("head")
    report = compare(0)
    assert report["removed"] == [retired]
    assert report["retired"] == {retired: [retained]} and not report["problems"]

    for options in ({"sha": "another-baseline"}, {"module": "test_same_name.py"},
                    {"old": "assert False"}, {"coverage": None},
                    {"coverage": "pytest.skip('not executed')"}, {"coverage": "assert False"}):
        run("base", **{"old": "pass", **options})
        assert not compare(1)["retired"]

    run("base", old="pass", unrelated=True)
    report = compare(1)
    assert report["retired"] == {retired: [retained]}
    assert report["removed"] == sorted([retired, "test_unapproved_removal"])
    assert report["problems"]

    run("base", old="pass")
    for coverage in (None, "pytest.skip('not executed')", "assert False"):
        run("head", coverage=coverage)
        assert not compare(1)["retired"]

    # Keeping the old name cannot use the retirement to bypass a failure or skip.
    for old, category in (("assert False", "regressions"), ("pytest.skip('not executed')", "new_skips")):
        run("head", old=old)
        report = compare(1)
        assert not report["retired"] and not report["removed"]
        assert report[category] == [retired]


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

        def compare(expected, *options):
            for suffix in (".md", ".json"):
                (root / "comparison").with_suffix(suffix).unlink(missing_ok=True)
            result = subprocess.run([
                sys.executable, str(RUNNER), "compare", "--reports", str(root / "reports"),
                "--output", str(root / "comparison"), "--platforms", "local", *options,
            ], check=False, stdout=subprocess.DEVNULL)
            assert (root / "comparison.md").exists(), "Missing readable failure summary"
            assert result.returncode == expected, (root / "comparison.md").read_text()
            return json.loads((root / "comparison.json").read_text())["platforms"].get("local", {})

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

        # Approved replacements must remain comparable, including a failing
        # replacement. Unknown renames must still fail as missing coverage.
        replacements = {
            "test_encrypted_thought_label_is_display_only_live_and_in_history":
                "test_unreadable_thoughts_are_hidden_live_and_in_history",
            "test_inline_activity_reuses_thought_header_and_keeps_prompt_before_reply":
                "test_inline_activity_is_separate_from_thought_folding_and_follows_prompt",
        }
        run("base", "\n".join(f"def {name}(): pass" for name in replacements))
        run("head", "\n".join(f"def {name}(): pass" for name in replacements.values())
            + "\ndef test_live_messages_are_visible_and_folds_are_independent(): pass\n")
        report = compare(0)
        assert report["added"] == ["test_live_messages_are_visible_and_folds_are_independent"]
        assert not report["removed"] and not report["problems"]
        run("head", "\n".join(f"def {name}(): assert False" for name in replacements.values()))
        report = compare(1)
        assert report["regressions"] == sorted(replacements)
        assert not report["removed"] and not report["newly_exposed_failures"]
        run("head", "def test_unapproved_replacement(): pass\n")
        assert compare(1)["removed"] == sorted(replacements)
        run("head", "\n".join(f"def {name}(): pass" for name in
                             [*replacements, *replacements.values()]))
        assert any("Ambiguous test name" in problem for problem in compare(1)["problems"])

        # PR #16 replaces startup/cancellation behavior and metadata precedence.
        # Cover all four collected identities, including both parameter values.
        catalog_names = {
            "base": ["test_catalog_refreshes_on_each_open_and_does_not_fallback_after_failure",
                     "test_escape_cancels_live_catalog_loading_without_opening_stale_picker",
                     "test_live_luna_reasoning_metadata_overrides_builtin_and_configured_defaults"],
            "head": ["test_catalog_refreshes_on_restart_and_keeps_cache_after_failure",
                     "test_picker_escape_does_not_cancel_background_startup_refresh",
                     "test_configured_luna_reasoning_metadata_overrides_live_defaults"],
        }

        def catalog_source(label, body="pass"):
            first, second, parameterized = catalog_names[label]
            return (f"import pytest\ndef {first}(): {body}\ndef {second}(): {body}\n"
                    f"@pytest.mark.parametrize('default', [None, 'max'])\n"
                    f"def {parameterized}(default): {body}\n")

        expected = sorted([*catalog_names["base"][:2], *[
            f"{catalog_names['base'][2]}[{value}]" for value in ("None", "max")
        ]])
        run("base", catalog_source("base"))
        run("head", catalog_source("head"))
        report = compare(0)
        assert not report["added"] and not report["removed"] and not report["problems"]
        run("head", catalog_source("head", "assert False"))
        report = compare(1)
        assert report["regressions"] == expected
        assert not report["removed"] and not report["newly_exposed_failures"]
        run("head", "def test_unknown_catalog_replacement(): pass\n")
        assert compare(1)["removed"] == expected
        run("head", catalog_source("head") + catalog_source("base"))
        assert any("Ambiguous test name" in problem for problem in compare(1)["problems"])

        run("head", "def test_duplicate(): pass\n", extra="def test_duplicate(): pass\n")
        assert compare(1)["problems"]
        (root / "head" / "test_other.py").unlink()
        run("head", "raise RuntimeError('collection failed')\n")
        assert compare(1)["problems"]
        (root / "reports" / "head" / "results.json").unlink()
        assert compare(1)["problems"]
        # Failure modes specified before implementation: incomplete or empty
        # reports accepted as green; malformed/duplicate JSON losing the summary;
        # wrong SHA, counts, statuses or unexpected matrix results being trusted;
        # main inheriting PR's existing-failure allowance; skipped/failed upstream
        # jobs being hidden by docs-only mode; collection skips counted twice.
        run("base", "def test_ok(): pass\n")
        run("head", "def test_ok(): pass\n")
        head_path = root / "reports" / "head" / "results.json"
        valid = json.loads(head_path.read_text())
        target_path = root / "target.json"
        target = {"pr": 10, "url": "https://github.com/owner/project/pull/10",
                  "base_sha": "base", "head_sha": "head", "mode": "comparison", "run_tests": True}
        target_path.write_text(json.dumps(target))
        compare(0, "--target", str(target_path))
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
            {**valid, "label": "unexpected"},
            {**valid, "duration": -1},
            {**valid, "collection_skips": "wrong"},
            {**valid, "problems": "wrong"},
            {}, [],
        ]
        for bad in bad_reports:
            head_path.write_text(json.dumps(bad))
            compare(1, "--target", str(target_path))
        for invalid in ('{', '{"platform":"local","platform":"other"}'):
            head_path.write_text(invalid)
            compare(1, "--target", str(target_path))
        head_path.write_text(json.dumps(valid))
        duplicate = root / "reports" / "duplicate" / "results.json"
        duplicate.parent.mkdir()
        duplicate.write_text(json.dumps(valid))
        compare(1, "--target", str(target_path))
        duplicate.unlink()
        for invalid in ('{', '{}', '[]'):
            target_path.write_text(invalid)
            compare(1, "--target", str(target_path))
        target_path.unlink()
        compare(1, "--target", str(target_path))
        for job in ("resolve", "checks", "test"):
            for status in ("failure", "cancelled", "skipped"):
                compare(1, f"--{job}-result", status)

        # A module-level skip remains a separate outcome, not a missing case.
        run("head", "def test_ok(): pass\n", extra="import pytest\npytest.skip('module', allow_module_level=True)\n")
        compare(0)
        (root / "head" / "test_other.py").unlink()
        (root / "reports" / "base" / "results.json").unlink()
        compare(0, "--mode", "head")
        run("head", "def test_failure(): assert False\n")
        report = compare(1, "--mode", "head")
        assert report["failures"] == ["test_failure"]
        run("head", "import pytest\n@pytest.mark.skip(reason='platform')\ndef test_skip(): pass\n")
        compare(0, "--mode", "head")

        # Documentation skips still require a valid target and successful checks.
        head_path.unlink()
        target.update(run_tests=False, skip_reason="Only documentation changed")
        target_path.write_text(json.dumps(target))
        compare(0, "--target", str(target_path), "--resolve-result", "success",
                "--checks-result", "success", "--test-result", "skipped")
        summary = json.loads((root / "comparison.json").read_text())
        assert summary["skipped"] and not summary["needs_attention"]
        assert "Only documentation changed" in (root / "comparison.md").read_text()
        compare(1, "--target", str(target_path), "--checks-result", "failure", "--test-result", "skipped")
        target["run_tests"] = "false"
        target_path.write_text(json.dumps(target))
        compare(1, "--target", str(target_path))
        check_retirements(root / "retirements")
    print("CLI end-to-end checks passed")


if __name__ == "__main__":
    main()
