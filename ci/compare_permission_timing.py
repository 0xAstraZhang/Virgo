"""Compare completed samples from the same workload and runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
from pathlib import Path

METRICS = ("cold_start_ms", "short_sequence_ms", "edit_to_test_ms")
BASELINES = {"no-limit": ("legacy-host", "98cc711cdb9bbb80a9ad9a2ae11c8d92f94ceec8"),
             "workspace-edit": ("legacy-native", "6346255146f11b8024e11fd3f3faae19f41d5862"),
             "read-only": ("legacy-native", "6346255146f11b8024e11fd3f3faae19f41d5862")}


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
        if sample.get("edit_actor") != actor or len(sample["operations"]) != report["repeats"] + 1:
            raise ValueError("The measured work disagrees with the requested comparison")
        for key in METRICS:
            if not isinstance(sample[key], (float, int)) or not math.isfinite(sample[key]) or sample[key] <= 0:
                raise ValueError(f"Invalid {key} sample")
    return {key: statistics.median(s[key] for s in samples) for key in METRICS}


def comparison(head, base, head_sha, mode):
    contract, base_sha = BASELINES[mode]
    for key in ("platform", "python", "script_sha256", "workload", "planned_samples", "repeats"):
        if head[key] != base[key]:
            raise ValueError(f"Baseline and head differ in {key}")
    baseline = validated(base, base_sha, contract, mode)
    current = validated(head, head_sha, "current", mode)
    return {"mode": mode, "head_revision": head_sha, "baseline_revision": base_sha,
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
    parser.add_argument("--mode", choices=list(BASELINES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
