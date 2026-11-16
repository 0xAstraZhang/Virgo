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


def check_product_retirements(root):
    """Retired products are explicit removals, never equivalent coverage aliases.

    Failure cases fixed before comparator changes: broad-name/module/parameter
    exemptions; another baseline inheriting an exception; a failed original
    disappearing; missing/failed/skipped or relocated preservation/acceptance
    gates; head-only migration checks accidentally required on the old revision;
    unrelated deletions, failures or new skips hidden by a retirement.
    """
    baseline = "47e1757bda5c5cfafb3a5a508a1a6ec4d629bfaf"
    retired = "test_goal_rejects_incomplete_or_malformed_verdicts[reply0]"
    module = "tests.features.test_goal"
    preservation = {
        "test_result_save_failure_delivers_result_and_blocks_restart": "tests.harness.test_harness_boundaries",
        "test_explicit_policy_preserves_order_and_fails_closed": "tests.harness.test_harness_boundaries",
        "test_background_job_notifies_idle_session": "tests.features.test_background",
        "test_subagent_fresh_context_and_no_recursion": "tests.features.test_subagent",
        "test_execution_rechecks_mode_arguments_and_symlink_boundary": "tests.features.test_permission",
    }
    acceptance = {name: "tests.harness.test_core_plugins_e2e" for name in (
        "test_minimal_coding_loop_and_resume_without_plugin_imports",
        "test_minimal_permission_rechecks_hook_rewritten_arguments",
        "test_owned_publication_preserves_the_running_batch",
        "test_failed_plugin_rolls_back_and_cleanup_continues",
        "test_project_extensions_require_trust_outside_the_project",
        "test_children_share_budget_permission_ceiling_and_cancellation",
        "test_retired_data_stays_untouched_and_history_remains_readable",
        "test_profile_layers_and_explicit_plugin_overrides",
    )}

    def run(label, *, old=None, original=retired, original_module=module, sha=None,
            changed=None, moved=None, unrelated=False):
        project = root / label
        shutil.rmtree(project, ignore_errors=True)
        project.mkdir(parents=True)
        identities = {**preservation, **(acceptance if label == "head" else {})}
        cases = {name: (path, "pass") for name, path in identities.items()}
        if old is not None:
            cases[original] = (original_module, old)
        if unrelated:
            cases["test_unapproved_product_deletion"] = (module, "pass")
        for name, body in (changed or {}).items():
            if body is None:
                cases.pop(name, None)
            else:
                cases[name] = (identities[name], body)
        for name in moved or ():
            cases[name] = ("test_wrong_module", cases[name][1])
        sources = {}
        for name, (path, body) in cases.items():
            function, separator, parameter = name.partition("[")
            decorator = f"@pytest.mark.parametrize('_case', [0], ids=[{parameter[:-1]!r}])\n" if separator else ""
            sources.setdefault(path, []).append(f"{decorator}def {function}({'_case' if separator else ''}): {body}\n")
        for path, functions in sources.items():
            target = project / (path.replace(".", "/") + ".py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("import pytest\n" + "\n".join(functions), encoding="utf-8")
        subprocess.run([
            sys.executable, str(RUNNER), "run", "--project", str(project),
            "--output", str(root / "reports" / label), "--platform", "local",
            "--label", label, "--sha", sha or (baseline if label == "base" else "head"),
        ], check=False, stdout=subprocess.DEVNULL)

    def compare(expected):
        result = subprocess.run([
            sys.executable, str(RUNNER), "compare", "--reports", str(root / "reports"),
            "--output", str(root / "comparison"), "--platforms", "local",
        ], check=False, stdout=subprocess.DEVNULL)
        text = (root / "comparison.md").read_text(encoding="utf-8")
        assert result.returncode == expected, text
        report = json.loads((root / "comparison.json").read_text(encoding="utf-8"))["platforms"]["local"]
        return report, text

    run("base", old="pass")
    run("head")
    report, text = compare(0)
    assert report["removed"] == [retired] and list(report["retired_products"]) == [retired]
    assert not report["retired"], "Removed products must not masquerade as duplicate coverage"
    assert "Reviewed retirements" in text and "retired product" in text and retired in text
    assert set(acceptance) <= set(report["added"]), "Migration checks exist only on head"

    for options in ({"sha": "other-baseline"}, {"original_module": "test_same_name"},
                    {"original": "test_goal_rejects_incomplete_or_malformed_verdicts[reply99]"},
                    {"old": "assert False"}):
        run("base", **{"old": "pass", **options})
        assert not compare(1)[0]["retired_products"]
    run("base", old="pytest.skip('old skip')")
    assert not compare(0)[0]["retired_products"]  # Preserve the general baseline-skipped removal rule.

    retained = next(iter(preservation))
    migration = "test_retired_data_stays_untouched_and_history_remains_readable"
    for label, gate in (("base", retained), ("head", retained), ("head", migration)):
        for body in (None, "assert False", "pytest.skip('not executed')"):
            run("base", old="pass")
            run("head")
            run(label, old="pass" if label == "base" else None, changed={gate: body})
            assert not compare(1)[0]["retired_products"]
        run("base", old="pass")
        run("head")
        run(label, old="pass" if label == "base" else None, moved=[gate])
        assert not compare(1)[0]["retired_products"]

    run("base", old="pass", unrelated=True)
    run("head")
    report, _ = compare(1)
    assert list(report["retired_products"]) == [retired] and report["problems"]
    run("base", old="pass")
    for old, category in (("assert False", "regressions"), ("pytest.skip('new skip')", "new_skips")):
        run("head", old=old)
        report, _ = compare(1)
        assert not report["retired_products"] and report[category] == [retired]


def check_execution_policy_retirements(root):
    """A second policy must not inherit another baseline's exceptions or gates.

    Reproduce before implementation: exactly reviewed removals are rejected by
    the old single-policy comparator. Wrong baselines/modules/parameters, failed
    originals, every missing gate, failed/skipped/relocated gates, unrelated
    removals and still-present failures must remain ordinary CI blockers.
    """
    baseline = "45bb6d52cffa4726e31329ba8f9c421b7b81d9a7"
    retired = "test_failed_review_is_not_a_safety_denial[reply0-invalid-json]"
    module = "tests.features.test_permission_verdicts"
    preservation = {
        "test_direct_main_event_binds_once_across_chat_switch": "tests.features.test_permission_context",
        "test_unbound_nonmain_event_cannot_borrow_main_authorization": "tests.features.test_permission_context",
        "test_background_subagent_captures_origin_before_worker_starts": "tests.features.test_permission_context",
        "test_execution_rechecks_mode_arguments_and_symlink_boundary": "tests.features.test_permission",
        "test_new_user_restriction_invalidates_session_approval": "tests.features.test_permission_verdicts",
        "test_permission_hook_errors_cannot_fall_through_event_bus": "tests.features.test_permission_verdicts",
        "test_background_job_notifies_idle_session": "tests.features.test_background",
        "test_children_share_budget_permission_ceiling_and_cancellation": "tests.harness.test_core_plugins_e2e",
    }
    acceptance = {name: "tests.features.test_permission_policy_e2e" for name in (
        "test_hard_sandbox_denial_does_not_request_approval",
        "test_never_rejects_unknown_operations_without_blocking_workspace_writes",
        "test_project_cannot_mark_a_mutating_command_read_only",
        "test_project_cannot_select_authorization_grants[permission0]",
        "test_project_cannot_select_authorization_grants[permission1]",
        "test_project_cannot_turn_user_deny_into_accept_all",
        "test_project_deny_rules_cannot_erase_user_restrictions[project_denies0]",
        "test_project_deny_rules_cannot_erase_user_restrictions[project_denies1]",
        "test_requested_config_cannot_pretend_host_shell_is_isolated",
    )}
    policy = json.loads((RUNNER.parent / "pipeline" / "execution_policy_retirements.json").read_text())
    inventory = {case["name"]: case for case in policy["retired_cases"]}
    assert policy["baseline_sha"] == baseline
    assert len(inventory) == len(policy["retired_cases"]) == 42
    assert inventory[retired]["classname"] == module

    def run(label, *, old=None, original=retired, original_module=module, sha=None,
            changed=None, moved=(), unrelated=None):
        project = root / label
        shutil.rmtree(project, ignore_errors=True)
        project.mkdir(parents=True)
        identities = {**preservation, **(acceptance if label == "head" else {})}
        cases = {name: (path, "pass") for name, path in identities.items()}
        if label == "base":
            cases.update({name: (case["classname"], "pass") for name, case in inventory.items()})
        if label == "base" or old is not None:
            cases.pop(retired, None)
            cases[original] = (original_module, old or "pass")
        if unrelated is not None:
            cases["test_unrelated_permission_guard"] = (module, unrelated)
        for name, body in (changed or {}).items():
            if body is None:
                cases.pop(name, None)
            else:
                cases[name] = (identities[name], body)
        for name in moved:
            cases[name] = ("test_wrong_module", cases[name][1])
        sources = {}
        for name, (path, body) in cases.items():
            function, separator, parameter = name.partition("[")
            rows = sources.setdefault(path, {}).setdefault(function, [])
            rows.append((parameter[:-1] if separator else None, body))
        for path, functions in sources.items():
            source = ["import pytest\n"]
            for function, rows in functions.items():
                if rows[0][0] is None:
                    assert len(rows) == 1
                    source.append(f"def {function}(): {rows[0][1]}\n")
                else:
                    parameters, bodies = map(list, zip(*rows, strict=True))
                    source.append(f"@pytest.mark.parametrize('_outcome', {bodies!r}, ids={parameters!r})\n"
                                  f"def {function}(_outcome): exec(_outcome)\n")
            target = project / (path.replace(".", "/") + ".py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(source), encoding="utf-8")
        subprocess.run([
            sys.executable, str(RUNNER), "run", "--project", str(project),
            "--output", str(root / "reports" / label), "--platform", "local",
            "--label", label, "--sha", sha or (baseline if label == "base" else "head"),
        ], check=False, stdout=subprocess.DEVNULL)

    def compare(expected):
        result = subprocess.run([
            sys.executable, str(RUNNER), "compare", "--reports", str(root / "reports"),
            "--output", str(root / "comparison"), "--platforms", "local",
        ], check=False, stdout=subprocess.DEVNULL)
        text = (root / "comparison.md").read_text(encoding="utf-8")
        assert result.returncode == expected, text
        report = json.loads((root / "comparison.json").read_text(encoding="utf-8"))["platforms"]["local"]
        return report, text

    run("base")
    run("head")
    report, text = compare(0)
    assert report["removed"] == sorted(inventory)
    assert report["retired_products"] == {
        name: {key: case[key] for key in ("classname", "product")} for name, case in inventory.items()
    }
    assert not report["retired"] and not report["problems"]
    assert policy["reason"] in text
    assert "The six official non-coding product subsystems are removed." not in text
    for name, path in {**preservation, **acceptance}.items():
        assert f"`{path}::{name}`" in text
    assert set(acceptance) <= set(report["added"])

    # Selecting the historical baseline must never union both policy inventories.
    for sha in ("other-baseline", "47e1757bda5c5cfafb3a5a508a1a6ec4d629bfaf"):
        run("base", sha=sha)
        report, text = compare(1)
        assert not report["retired_products"] and policy["reason"] not in text
    for options in ({"original_module": "test_same_name"},
                    {"original": "test_failed_review_is_not_a_safety_denial[reply99-invalid-json]"},
                    {"old": "assert False"}):
        run("base", **options)
        assert retired not in compare(1)[0]["retired_products"]
    run("base", old="pytest.skip('old skip')")
    assert retired not in compare(0)[0]["retired_products"]

    # Each declared safety/acceptance gate is required, at the exact revision.
    gates = [(label, name) for label in ("base", "head") for name in preservation]
    gates += [("head", name) for name in acceptance]
    for label, gate in gates:
        run("base")
        run("head")
        run(label, changed={gate: None})
        assert not compare(1)[0]["retired_products"]
    for label, gate in (("base", next(iter(preservation))), ("head", next(iter(preservation))),
                        ("head", next(iter(acceptance)))):
        for options in ({"changed": {gate: "assert False"}},
                        {"changed": {gate: "pytest.skip('not executed')"}}, {"moved": [gate]}):
            run("base")
            run("head")
            run(label, **options)
            assert not compare(1)[0]["retired_products"]

    run("base", unrelated="pass")
    run("head")
    report, _ = compare(1)
    assert set(report["retired_products"]) == set(inventory) and report["problems"]
    assert "test_unrelated_permission_guard" in report["removed"]
    for body, category in (("assert False", "regressions"), ("pytest.skip('new skip')", "new_skips")):
        run("head", unrelated=body)
        assert compare(1)[0][category] == ["test_unrelated_permission_guard"]
    run("base")
    for old, category in (("assert False", "regressions"), ("pytest.skip('new skip')", "new_skips")):
        run("head", old=old)
        report, _ = compare(1)
        assert retired not in report["retired_products"] and retired not in report["removed"]
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
        check_product_retirements(root / "product-retirements")
        check_execution_policy_retirements(root / "execution-policy-retirements")
    print("CLI end-to-end checks passed")


if __name__ == "__main__":
    main()
