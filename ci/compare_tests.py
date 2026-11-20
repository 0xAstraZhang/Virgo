"""Resolve, run and summarize real pytest suites at immutable event revisions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.comparison import compare
from pipeline.execution import run
from pipeline.targets import classify, plan_matrix, resolve, stage_cache

PLATFORMS = ["ubuntu-latest", "windows-latest", "macos-latest"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("resolve")
    classifier = subparsers.add_parser("classify")
    classifier.add_argument("--target", type=Path, required=True)
    classifier.add_argument("--project", type=Path, default=Path("."))
    planner = subparsers.add_parser("plan-matrix")
    planner.add_argument("--target", type=Path, required=True)
    planner.add_argument("--cache-dir", type=Path, required=True)
    planner.add_argument("--reused-dir", type=Path, required=True)
    planner.add_argument("--platforms", nargs="+", default=PLATFORMS)
    stager = subparsers.add_parser("stage-cache")
    stager.add_argument("--report", type=Path, required=True)
    stager.add_argument("--destination", type=Path, required=True)
    stager.add_argument("--platform", required=True)
    stager.add_argument("--sha", required=True)
    runner = subparsers.add_parser("run")
    runner.add_argument("--project", type=Path, required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--platform", required=True)
    runner.add_argument("--label", choices=("base", "head"), required=True)
    runner.add_argument("--sha", required=True)
    runner.add_argument("--execution", choices=("serial", "parallel"), default="parallel")
    runner.add_argument("--ignore", action="append", default=[])
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
    if args.command == "plan-matrix":
        return plan_matrix(args)
    if args.command == "stage-cache":
        return stage_cache(args)
    return run(args) if args.command == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
