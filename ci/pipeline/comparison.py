"""Compare revision outcomes and enforce the final CI result."""

from __future__ import annotations

import os

from .io import read_json, write_json
from .reports import BAD, approved_retirements, validate_report


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
        result["retired"] = {}
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
            result["retired"] = approved_retirements(base, head)
            missing_executed = [name for name in result["removed"]
                                if before[name] != "skipped" and name not in result["retired"]]
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
        markdown += [f"### Reviewed duplicate retirements: {len(result['retired'])}", ""]
        markdown += [f"- `{name}` — covered by " + ", ".join(f"`{case}`" for case in coverage)
                     for name, coverage in sorted(result["retired"].items())] or ["None."]
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
               "No new failures, unapproved test removals, or new skips. Existing failures and reviewed retirements are listed above.")
    markdown += ["## Result", "", "Needs attention; see the problems above." if failed else success, ""]
    write_json(args.output.with_suffix(".json"), combined)
    rendered = "\n".join(markdown)
    args.output.with_suffix(".md").write_text(rendered, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered)
    return int(failed)
