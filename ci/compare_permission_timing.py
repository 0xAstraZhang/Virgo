"""Compare completed samples from the same workload and runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
from pathlib import Path

METRICS = ("cold_start_ms", "first_command_ms", "short_sequence_ms", "edit_to_test_ms")
BASELINE = "04e94ed45992d8e2cbf2178c8c6b562704092a83"
MODES = ("read-only", "workspace-edit", "no-limit")


def validated(report, revision, contract, mode):
    if (report["revision"], report["contract"], report["mode"]) != (revision, contract, mode):
        raise ValueError("Revision, contract or mode disagrees with the requested comparison")
    if report["tracked_diff_sha256"] != hashlib.sha256(b"").hexdigest():
        raise ValueError("The measured checkout has tracked modifications")
    samples = report["samples"]
    if (report["status"] != "finished" or not samples or len(samples) != report["planned_samples"]
            or any(s.get("success") is not True or s.get("approval_questions") != [] for s in samples)):
        raise ValueError(f"Check the {contract} raw samples for failures or approvals")
    actor = "external_editor" if mode == "read-only" else "virgo_file_tool"
    for sample in samples:
        if (sample.get("project_context_loaded") is not True
                or sample.get("edit_actor") != actor or len(sample["operations"]) != report["repeats"] + 1
                or any(op.get("success") is not True for op in sample["operations"])
                or sample.get("shell_network_allowed") is not (mode != "read-only")):
            raise ValueError("The measured work disagrees with the requested comparison")
        for key in METRICS:
            if not isinstance(sample[key], (float, int)) or not math.isfinite(sample[key]) or sample[key] <= 0:
                raise ValueError(f"Invalid {key} sample")
    return {key: statistics.median(s[key] for s in samples) for key in METRICS}


def comparison(head, base, head_sha, mode):
    for key in ("platform", "python", "script_sha256", "workload", "planned_samples", "repeats"):
        if head[key] != base[key]:
            raise ValueError(f"Baseline and head differ in {key}")
    baseline = validated(base, BASELINE, "original", mode)
    current = validated(head, head_sha, "current", mode)
    return {"mode": mode, "head_revision": head_sha, "baseline_revision": BASELINE,
            "platform": head["platform"], "samples_per_revision": head["planned_samples"],
            "metrics": {key: {"baseline_median_ms": baseline[key], "head_median_ms": current[key],
                              "head_minus_baseline_ms": current[key] - baseline[key],
                              "head_over_baseline": current[key] / baseline[key]}
                        for key in METRICS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.head_sha == BASELINE:
        raise SystemExit("Head and baseline are the same revision; the comparison would be vacuous")
    report = comparison(json.loads(args.head.read_bytes()), json.loads(args.baseline.read_bytes()),
                        args.head_sha, args.mode)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(f"### {args.mode} on {report['platform']}\n\n")
            stream.write(f"Baseline `{report['baseline_revision']}`, head `{report['head_revision']}`.\n\n")
            stream.write("| Metric | Baseline median (ms) | Head median (ms) | Head / baseline |\n")
            stream.write("| --- | ---: | ---: | ---: |\n")
            for key, metric in report["metrics"].items():
                stream.write(f"| {key} | {metric['baseline_median_ms']:.1f} | "
                             f"{metric['head_median_ms']:.1f} | {metric['head_over_baseline']:.3f} |\n")


if __name__ == "__main__":
    main()
