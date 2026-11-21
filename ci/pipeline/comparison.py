"""Validate per-platform head test reports and enforce the final CI gate."""

from __future__ import annotations

import os

from .io import read_json, write_json
from .reports import BAD, validate_report


def summarize(args):
    reports, problems, target = {}, [], None
    report_problems = {operating_system: [] for operating_system in args.platforms}
    skipped = False
    if args.target:
        try:
            candidate = read_json(args.target)
            if not isinstance(candidate, dict) or not all(
                isinstance(candidate.get(key), str) and candidate[key]
                for key in ("base_sha", "head_sha", "url")
            ):
                raise ValueError("Missing target identity")
            if type(candidate.get("run_tests", True)) is not bool:
                raise ValueError("Invalid run_tests decision")
            skipped = candidate.get("run_tests") is False
            if skipped and (not isinstance(candidate.get("skip_reason"), str) or not candidate["skip_reason"]):
                raise ValueError("Missing skip reason")
            target = candidate
        except (OSError, ValueError, TypeError) as error:
            skipped = False
            problems.append(f"Cannot read target: {error}")

    for job in ("resolve", "checks", "test"):
        status = getattr(args, f"{job}_result")
        expected = "skipped" if job == "test" and skipped else "success"
        if status is not None and status != expected:
            problems.append(f"{job} job: {status} (expected {expected})")

    for path in args.reports.rglob("results.json"):
        data = None
        try:
            data = read_json(path)
            validate_report(data)
            operating_system = data["platform"]
            if skipped or operating_system not in args.platforms:
                raise ValueError(f"Unexpected run report: {operating_system}")
            if operating_system in reports:
                raise ValueError(f"Duplicate run report: {operating_system}")
            reports[operating_system] = data
        except (OSError, ValueError, TypeError) as error:
            problems.append(f"{path.relative_to(args.reports)}: {error}")
            # Preserve useful producer diagnostics even when validation rejects
            # the report (e.g. a name collision also makes counts inconsistent).
            if isinstance(data, dict) and isinstance(data.get("platform"), str) and data["platform"] in report_problems:
                details = report_problems[data["platform"]]
                details.append(str(error))
                if isinstance(data.get("problems"), list):
                    details.extend(message for message in data["problems"] if isinstance(message, str))

    combined = {"target": target, "skipped": skipped, "problems": problems, "platforms": {}}
    markdown = ["# Cross-platform CI", ""]
    if target:
        title = f"PR #{target['pr']}" if target.get("pr") else "Main commit"
        markdown += [f"[{title}]({target['url']})", "", f"Commit: `{target['head_sha']}`", ""]
    if skipped:
        markdown += [f"Full tests skipped: {target['skip_reason']}.", ""]
    markdown += [
        "| Platform | Passed | Failed | Errors | Skipped | Seconds |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for operating_system in ([] if skipped else args.platforms):
        report = reports.get(operating_system)
        if report:
            counts = report["counts"]
            values = [counts.get(s, 0) for s in ("passed", "failed", "error", "skipped")]
            markdown.append(f"| {operating_system} | " + " | ".join(map(str, values)) + f" | {report['duration']} |")
        else:
            markdown.append(f"| {operating_system} | missing | — | — | — | — |")

    failed = bool(problems)
    for operating_system in ([] if skipped else args.platforms):
        report = reports.get(operating_system)
        result = {"failures": [], "problems": list(report_problems[operating_system])}
        combined["platforms"][operating_system] = result
        if not report:
            result["problems"].append("Missing test report")
        else:
            result["problems"].extend(report["problems"])
            if target and report["sha"] != target["head_sha"]:
                result["problems"].append("Tested SHA does not match resolved target")
            result["failures"] = sorted(name for name, case in report["tests"].items() if case["status"] in BAD)
            result["counts"] = report["counts"]
            result["collection_skips"] = report["collection_skips"]
        result["problems"].sort()
        failed |= bool(result["problems"] or result["failures"])
        markdown += ["", f"## {operating_system}", ""]
        for key, title in (
            ("problems", "Incomplete or ambiguous results"),
            ("failures", "Failed tests"),
        ):
            markdown += [f"### {title}: {len(result[key])}", ""]
            markdown += [f"- `{name}`" for name in result[key]] or ["None."]
            markdown.append("")
        if report:
            markdown += [
                f"Module-level collection skips: {', '.join(report['collection_skips']) or 'none'}",
                "",
            ]

    combined["needs_attention"] = failed
    if problems:
        markdown += ["## Workflow / report problems", "", *(f"- {problem}" for problem in problems), ""]
    success = (
        "Lightweight checks passed; full tests were intentionally skipped."
        if skipped
        else "All executed tests passed."
    )
    markdown += ["## Result", "", "Needs attention; see the problems above." if failed else success, ""]
    write_json(args.output.with_suffix(".json"), combined)
    rendered = "\n".join(markdown)
    args.output.with_suffix(".md").write_text(rendered, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered)
    return int(failed)
