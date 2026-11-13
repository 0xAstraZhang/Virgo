"""Resolve, run and summarize real pytest suites at immutable event revisions."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

PLATFORMS = ["ubuntu-latest", "windows-latest", "macos-latest"]
BAD = {"failed", "error"}
STATUSES = BAD | {"passed", "skipped"}
PARAMETER_ALIASES = {
    "test_compaction_does_not_enable_native_search[openai-responses]":
        "test_compaction_does_not_enable_native_search[virgo.ai.providers.responses]",
    "test_compaction_does_not_enable_native_search[openai-codex]":
        "test_compaction_does_not_enable_native_search[virgo.ai.providers.codex]",
    "test_subagent_request_exposes_only_its_own_tools":
        "test_subagent_system_prompt_lists_only_its_own_tools",
    "test_noninteractive_sessions_report_no_answer_without_a_prompt":
        "test_noninteractive_sessions_get_a_fallback_without_a_prompt",
    "test_runs_woken_by_notifications_time_out_without_an_answer":
        "test_runs_woken_by_notifications_time_out_to_the_recommendation",
    "test_parallel_todos_have_no_size_cap_or_stop_continuation":
        "test_todo_validation_and_stop_reminder",
    "test_unreadable_thoughts_are_hidden_live_and_in_history":
        "test_encrypted_thought_label_is_display_only_live_and_in_history",
    "test_inline_activity_is_separate_from_thought_folding_and_follows_prompt":
        "test_inline_activity_reuses_thought_header_and_keeps_prompt_before_reply",
    "test_catalog_refreshes_on_restart_and_keeps_cache_after_failure":
        "test_catalog_refreshes_on_each_open_and_does_not_fallback_after_failure",
    "test_picker_escape_does_not_cancel_background_startup_refresh":
        "test_escape_cancels_live_catalog_loading_without_opening_stale_picker",
    "test_configured_luna_reasoning_metadata_overrides_live_defaults[None]":
        "test_live_luna_reasoning_metadata_overrides_builtin_and_configured_defaults[None]",
    "test_configured_luna_reasoning_metadata_overrides_live_defaults[max]":
        "test_live_luna_reasoning_metadata_overrides_builtin_and_configured_defaults[max]",
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def outputs(values):
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                value = value if isinstance(value, str) else json.dumps(value)
                if "\n" in value or "\r" in value:
                    raise ValueError(f"Multiline workflow output: {key}")
                stream.write(f"{key}={value}\n")


def resolve():
    repository = os.environ["GITHUB_REPOSITORY"]
    number = os.environ.get("PR_NUMBER", "").strip()
    event_name = os.environ["GITHUB_EVENT_NAME"]

    def api(endpoint):
        return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True, encoding="utf-8"))

    if event_name == "push":
        event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
        target = {
            "pr": None, "url": f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/commit/{event['after']}",
            "base_sha": event["before"], "head_sha": event["after"],
            "base_repository": repository, "head_repository": repository,
        }
        if os.environ.get("GITHUB_SHA") != target["head_sha"]:
            raise ValueError("Push event SHA does not match GITHUB_SHA")
    elif event_name == "pull_request":
        # A queued run or rerun must keep the revisions that triggered it, even
        # when the PR's live refs have advanced. Never fall back to the API.
        event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
        pr = event["pull_request"]
        number = str(pr["number"])
    elif event_name == "workflow_dispatch":
        if not number:
            candidates = api(f"repos/{repository}/pulls?state=open&sort=created&direction=desc&per_page=100")
            if not candidates:
                raise SystemExit("No open PR to compare; supply a PR number.")
            number = str(candidates[0]["number"])
        if not re.fullmatch(r"[1-9][0-9]*", number):
            raise SystemExit("PR number must be a positive integer")
        pr = api(f"repos/{repository}/pulls/{number}")
    else:
        raise ValueError(f"Unsupported event: {event_name}")
    if event_name != "push":
        target = {
            "pr": int(number), "url": pr["html_url"],
            "base_sha": pr["base"]["sha"], "head_sha": pr["head"]["sha"],
            "base_repository": pr["base"]["repo"]["full_name"],
            "head_repository": pr["head"]["repo"]["full_name"],
        }
    for label in ("base", "head"):
        sha, repo = target[f"{label}_sha"], target[f"{label}_repository"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or (sha == "0" * 40 and (label != "base" or event_name != "push")):
            raise ValueError(f"Invalid {label} SHA")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"Invalid {label} repository")
    target.update(event=event_name, mode="head" if event_name == "push" else "comparison")
    write_json(Path("target.json"), target)
    outputs({**target, "revisions": ["head"] if target["mode"] == "head" else ["base", "head"]})
    print(json.dumps(target, indent=2))


def classify(args):
    target = read_json(args.target)
    run_tests, reason = True, "Manual runs always execute the complete suite"
    if target["event"] != "workflow_dispatch":
        reason = "Changes unavailable or empty; running the complete suite"
        try:
            # --no-renames includes both sides of a rename; -z preserves odd filenames.
            result = subprocess.run([
                "git", "-C", str(args.project), "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
                target["base_sha"], target["head_sha"], "--",
            ], capture_output=True, check=True, timeout=30)
            paths = result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []
            if paths:
                run_tests = not all(path == "README.md" or (path.startswith("docs/") and path.endswith(".md"))
                                    for path in paths)
                reason = "Code or configuration changed" if run_tests else "Only documentation changed"
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
    target.update(run_tests=run_tests, skip_reason=reason)
    write_json(args.target, target)
    outputs({"run_tests": run_tests, "skip_reason": reason})
    print(reason)


def run(args):
    project, output = args.project.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    junit = output / "junit.xml"
    junit.unlink(missing_ok=True)
    (output / "results.json").unlink(missing_ok=True)
    started = time.monotonic()
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        result = subprocess.run([
            sys.executable, str(Path(__file__).with_name("run_pytest.py").resolve()),
            "-q", "-ra", "--color=no",
            f"--junitxml={junit}",
        ], cwd=project, stdout=log, stderr=subprocess.STDOUT, check=False)
    report = {
        "platform": args.platform, "label": args.label, "sha": args.sha,
        "python": sys.version, "system": platform.platform(),
        "exit_code": result.returncode, "duration": round(time.monotonic() - started, 2),
        "complete": result.returncode in {0, 1}, "counts": {}, "tests": {},
        "collection_skips": [], "problems": [],
    }
    counts = Counter()
    try:
        for case in ET.parse(junit).iter("testcase"):
            name = case.attrib["name"]
            status = next((tag for tag in ("error", "failure", "skipped") if case.find(tag) is not None), "passed")
            status = "failed" if status == "failure" else status
            counts[status] += 1
            key = PARAMETER_ALIASES.get(name, name)
            if not name.startswith("test_"):
                if status == "skipped":
                    report["collection_skips"].append(name)
                    continue
                report["problems"].append(f"Collection problem: {name}")
            if key in report["tests"]:
                report["problems"].append(f"Ambiguous test name: {key}")
            report["tests"][key] = {"status": status, "classname": case.get("classname", "")}
        if not counts:
            report["problems"].append("No test outcomes in JUnit report")
    except (OSError, ET.ParseError) as error:
        report["problems"].append(f"Cannot read JUnit report: {error}")
    if not report["complete"]:
        report["problems"].append(f"pytest did not complete normally (exit {result.returncode})")
    report["counts"] = dict(counts)
    write_json(output / "results.json", report)
    print((output / "pytest.log").read_text(encoding="utf-8", errors="replace"))
    return result.returncode if result.returncode else int(bool(report["problems"]))


def validate_report(data):
    if not isinstance(data, dict):
        raise ValueError("Report must be an object")
    for key in ("platform", "label", "sha"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Missing or invalid {key}")
    if data.get("complete") is not True or type(data.get("exit_code")) is not int or data["exit_code"] not in {0, 1}:
        raise ValueError("pytest did not complete normally")
    duration = data.get("duration")
    if type(duration) not in {float, int} or not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid duration")
    for key in ("problems", "collection_skips"):
        if not isinstance(data.get(key), list) or not all(isinstance(value, str) for value in data[key]):
            raise ValueError(f"Invalid {key}")
    if not isinstance(data.get("tests"), dict) or not data["tests"]:
        raise ValueError("No individual test outcomes in report")
    counts = Counter()
    for name, case in data["tests"].items():
        if not name or not isinstance(case, dict) or case.get("status") not in STATUSES:
            raise ValueError(f"Invalid test outcome: {name}")
        counts[case["status"]] += 1
    counts["skipped"] += len(data["collection_skips"])
    actual = data.get("counts")
    if (not isinstance(actual, dict) or any(key not in STATUSES or type(value) is not int or value < 0
                                          for key, value in actual.items()) or Counter(actual) != counts):
        raise ValueError("Test counts do not match individual outcomes")
    if bool(data["exit_code"]) != bool(counts["failed"] + counts["error"]):
        raise ValueError("pytest exit code does not match test outcomes")


def compare(args):
    reports, problems, target = {}, [], None
    report_problems = {operating_system: [] for operating_system in args.platforms}
    mode, skipped = args.mode or "comparison", False
    if args.target:
        try:
            candidate = read_json(args.target)
            if not isinstance(candidate, dict) or not all(isinstance(candidate.get(key), str) and candidate[key]
                                                         for key in ("base_sha", "head_sha", "url")):
                raise ValueError("Missing target identity")
            target_mode = candidate.get("mode", "comparison")
            if target_mode not in {"comparison", "head"} or (args.mode and args.mode != target_mode):
                raise ValueError("Invalid or conflicting target mode")
            if type(candidate.get("run_tests", True)) is not bool:
                raise ValueError("Invalid run_tests decision")
            skipped = candidate.get("run_tests") is False
            if skipped and (not isinstance(candidate.get("skip_reason"), str) or not candidate["skip_reason"]):
                raise ValueError("Missing skip reason")
            target, mode = candidate, target_mode
        except (OSError, ValueError, TypeError) as error:
            skipped = False
            problems.append(f"Cannot read target: {error}")
    for job in ("resolve", "checks", "test"):
        status = getattr(args, f"{job}_result")
        expected = "skipped" if job == "test" and skipped else "success"
        if status is not None and status != expected:
            problems.append(f"{job} job: {status} (expected {expected})")
    labels = ("base", "head") if mode == "comparison" else ("head",)
    for path in args.reports.rglob("results.json"):
        data = None
        try:
            data = read_json(path)
            validate_report(data)
            key = data["platform"], data["label"]
            if skipped or key[0] not in args.platforms or key[1] not in labels:
                raise ValueError(f"Unexpected run report: {key}")
            if key in reports:
                raise ValueError(f"Duplicate run report: {key}")
            reports[key] = data
        except (OSError, ValueError, TypeError) as error:
            problems.append(f"{path.relative_to(args.reports)}: {error}")
            # Preserve useful producer diagnostics even when validation rejects
            # the report (e.g. a name collision also makes counts inconsistent).
            if isinstance(data, dict) and isinstance(data.get("platform"), str) and data["platform"] in report_problems:
                details = report_problems[data["platform"]]
                details.append(str(error))
                if isinstance(data.get("problems"), list):
                    details.extend(message for message in data["problems"] if isinstance(message, str))
    combined = {"target": target, "mode": mode, "skipped": skipped, "problems": problems, "platforms": {}}
    markdown = ["# Cross-platform CI", ""]
    if target:
        title = f"PR #{target['pr']}" if target.get("pr") else "Main commit"
        markdown += [f"[{title}]({target['url']})", "",
                     f"Base: `{target['base_sha']}`  ", f"Head: `{target['head_sha']}`", ""]
    if skipped:
        markdown += [f"Full tests skipped: {target['skip_reason']}.", ""]
    markdown += ["| Platform | Revision | Passed | Failed | Errors | Skipped | Seconds |",
                 "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for operating_system in ([] if skipped else args.platforms):
        for label in labels:
            report = reports.get((operating_system, label))
            if report:
                counts = report["counts"]
                values = [counts.get(s, 0) for s in ("passed", "failed", "error", "skipped")]
                markdown.append(f"| {operating_system} | {label} | " + " | ".join(map(str, values))
                                + f" | {report['duration']} |")
            else:
                markdown.append(f"| {operating_system} | {label} | missing | — | — | — | — |")
    failed = bool(problems)
    for operating_system in ([] if skipped else args.platforms):
        base, head = (reports.get((operating_system, label)) for label in ("base", "head"))
        result = {key: [] for key in (
            "regressions", "newly_exposed_failures", "existing_failures", "fixed_failures",
            "new_skips", "unskipped", "added", "removed", "problems", "failures",
        )}
        combined["platforms"][operating_system] = result
        result["problems"].extend(report_problems[operating_system])
        for label in labels:
            report = reports.get((operating_system, label))
            if not report:
                result["problems"].append(f"Missing {label} report")
                continue
            result["problems"].extend(f"{label}: {message}" for message in report["problems"])
            if target and report["sha"] != target[f"{label}_sha"]:
                result["problems"].append(f"{label}: tested SHA does not match resolved target")
        if mode == "head" and head:
            result["failures"] = [name for name, case in head["tests"].items() if case["status"] in BAD]
            result["counts"] = {"head": head["counts"]}
            result["collection_skips"] = {"head": head["collection_skips"]}
        if mode == "comparison" and base and head:
            before = {key: value["status"] for key, value in base["tests"].items()}
            after = {key: value["status"] for key, value in head["tests"].items()}
            for name, status in after.items():
                old = before.get(name)
                if status in BAD:
                    category = "existing_failures" if old in BAD else (
                        "regressions" if old == "passed" else "newly_exposed_failures"
                    )
                    result[category].append(name)
                if old in BAD and status == "passed":
                    result["fixed_failures"].append(name)
                if status == "skipped" and old not in {None, "skipped"}:
                    result["new_skips"].append(name)
                if old == "skipped" and status != "skipped":
                    result["unskipped"].append(name)
            result["added"] = sorted(after.keys() - before.keys())
            result["removed"] = sorted(before.keys() - after.keys())
            missing_executed = [name for name in result["removed"] if before[name] != "skipped"]
            if missing_executed:
                result["problems"].append(f"{len(missing_executed)} previously executed tests disappeared")
            result["counts"] = {"base": base["counts"], "head": head["counts"]}
            result["collection_skips"] = {"base": base["collection_skips"], "head": head["collection_skips"]}
        for value in result.values():
            if isinstance(value, list):
                value.sort()
        failed |= any(result[key] for key in ("regressions", "newly_exposed_failures", "problems", "new_skips", "failures"))
        markdown += ["", f"## {operating_system}", ""]
        for key, title in (
            ("problems", "Incomplete or ambiguous results"), ("failures", "Failed tests"),
            ("regressions", "New failures (baseline passed)"),
            ("newly_exposed_failures", "Newly exposed failures (baseline skipped or absent)"),
            ("existing_failures", "Existing failures"), ("fixed_failures", "Fixed failures"),
            ("new_skips", "New skips"), ("unskipped", "Previously skipped, now executed"),
            ("added", "Added / newly collected tests"), ("removed", "Removed / no longer collected tests"),
        ):
            markdown += [f"### {title}: {len(result[key])}", ""]
            markdown += [f"- `{name}`" for name in result[key]] or ["None."]
            markdown.append("")
        if base and head:
            markdown += ["Module-level collection skips:", "",
                         f"- Base: {', '.join(base['collection_skips']) or 'none'}",
                         f"- Head: {', '.join(head['collection_skips']) or 'none'}", ""]
    combined["needs_attention"] = failed
    if problems:
        markdown += ["## Workflow / report problems", "", *(f"- {problem}" for problem in problems), ""]
    success = ("Lightweight checks passed; full tests were intentionally skipped." if skipped else
               "All executed tests passed." if mode == "head" else
               "No new failures, lost executed tests, or new skips. Existing failures are listed above.")
    markdown += ["## Result", "", "Needs attention; see the problems above." if failed else success, ""]
    write_json(args.output.with_suffix(".json"), combined)
    rendered = "\n".join(markdown)
    args.output.with_suffix(".md").write_text(rendered, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered)
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("resolve")
    classifier = subparsers.add_parser("classify")
    classifier.add_argument("--target", type=Path, required=True)
    classifier.add_argument("--project", type=Path, default=Path("."))
    runner = subparsers.add_parser("run")
    runner.add_argument("--project", type=Path, required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--platform", required=True)
    runner.add_argument("--label", choices=("base", "head"), required=True)
    runner.add_argument("--sha", required=True)
    comparer = subparsers.add_parser("compare")
    comparer.add_argument("--reports", type=Path, required=True)
    comparer.add_argument("--output", type=Path, required=True)
    comparer.add_argument("--platforms", nargs="+", default=PLATFORMS)
    comparer.add_argument("--target", type=Path)
    comparer.add_argument("--mode", choices=("comparison", "head"))
    for job in ("resolve", "checks", "test"):
        comparer.add_argument(f"--{job}-result")
    args = parser.parse_args()
    if args.command == "resolve":
        return resolve()
    if args.command == "classify":
        return classify(args)
    return run(args) if args.command == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
