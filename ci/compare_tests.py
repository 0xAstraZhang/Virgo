"""Run and compare real pytest suites across immutable PR revisions."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

PLATFORMS = ["ubuntu-latest", "windows-latest", "macos-latest"]
BAD = {"failed", "error"}
PARAMETER_ALIASES = {
    "test_compaction_does_not_enable_native_search[openai-responses]":
        "test_compaction_does_not_enable_native_search[virgo.ai.providers.responses]",
    "test_compaction_does_not_enable_native_search[openai-codex]":
        "test_compaction_does_not_enable_native_search[virgo.ai.providers.codex]",
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def resolve():
    repository = os.environ["GITHUB_REPOSITORY"]
    number = os.environ.get("PR_NUMBER", "").strip()

    def api(endpoint):
        return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True, encoding="utf-8"))

    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        # A queued run or rerun must keep the revisions that triggered it, even
        # when the PR's live refs have advanced. Never fall back to the API.
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pr = event["pull_request"]
        number = str(pr["number"])
    else:
        if not number:
            candidates = api(f"repos/{repository}/pulls?state=open&sort=created&direction=desc&per_page=100")
            candidates = [pr for pr in candidates if pr["head"]["ref"] != "codex/cross-platform-ci"]
            if not candidates:
                raise SystemExit("No open PR to compare; supply a PR number.")
            number = str(candidates[0]["number"])
        if not number.isdecimal():
            raise SystemExit("PR number must be an integer")
        pr = api(f"repos/{repository}/pulls/{number}")
    target = {
        "pr": int(number), "url": pr["html_url"],
        "base_sha": pr["base"]["sha"], "head_sha": pr["head"]["sha"],
        "base_repository": pr["base"]["repo"]["full_name"],
        "head_repository": pr["head"]["repo"]["full_name"],
    }
    write_json(Path("target.json"), target)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in target.items():
            output.write(f"{key}={value}\n")
    print(json.dumps(target, indent=2))


def run(args):
    project, output = args.project.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    junit = output / "junit.xml"
    junit.unlink(missing_ok=True)
    started = time.monotonic()
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        result = subprocess.run([
            sys.executable, "-m", "pytest", "-q", "-ra", "--color=no",
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


def compare(args):
    reports = {}
    for path in args.reports.rglob("results.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        key = data["platform"], data["label"]
        if key in reports:
            raise SystemExit(f"Duplicate run report: {key}")
        reports[key] = data
    target = json.loads(args.target.read_text(encoding="utf-8")) if args.target else None
    combined = {"target": target, "platforms": {}}
    markdown = ["# Cross-platform PR comparison", ""]
    if target:
        markdown += [f"[PR #{target['pr']}]({target['url']})", "",
                     f"Base: `{target['base_sha']}`  ", f"Head: `{target['head_sha']}`", ""]
    markdown += ["| Platform | Revision | Passed | Failed | Errors | Skipped | Seconds |",
                 "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for operating_system in args.platforms:
        for label in ("base", "head"):
            report = reports.get((operating_system, label))
            if report:
                counts = report["counts"]
                values = [counts.get(s, 0) for s in ("passed", "failed", "error", "skipped")]
                markdown.append(f"| {operating_system} | {label} | " + " | ".join(map(str, values))
                                + f" | {report['duration']} |")
            else:
                markdown.append(f"| {operating_system} | {label} | missing | — | — | — | — |")
    failed = False
    for operating_system in args.platforms:
        base, head = (reports.get((operating_system, label)) for label in ("base", "head"))
        result = {key: [] for key in (
            "regressions", "newly_exposed_failures", "existing_failures", "fixed_failures",
            "new_skips", "unskipped", "added", "removed", "problems",
        )}
        combined["platforms"][operating_system] = result
        for label, report in (("base", base), ("head", head)):
            if not report:
                result["problems"].append(f"Missing {label} report")
                continue
            result["problems"].extend(f"{label}: {message}" for message in report["problems"])
            if target and report["sha"] != target[f"{label}_sha"]:
                result["problems"].append(f"{label}: tested SHA does not match resolved target")
        if base and head:
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
        failed |= any(result[key] for key in ("regressions", "newly_exposed_failures", "problems", "new_skips"))
        markdown += ["", f"## {operating_system}", ""]
        for key, title in (
            ("problems", "Incomplete or ambiguous results"), ("regressions", "New failures (baseline passed)"),
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
    markdown += ["## Result", "", "Needs attention; see the differences above." if failed else
                 "No new failures, lost executed tests, or new skips. Existing failures are listed above.", ""]
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
    args = parser.parse_args()
    if args.command == "resolve":
        return resolve()
    return run(args) if args.command == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
