"""Resolve, run, and summarize real pytest suites at immutable event revisions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.comparison import summarize
from pipeline.execution import run
from pipeline.targets import classify, resolve

PLATFORMS = ["ubuntu-latest", "windows-latest", "macos-latest"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("resolve")
    classifier = subparsers.add_parser("classify")
    classifier.add_argument("--target", type=Path, required=True)
    classifier.add_argument("--project", type=Path, default=Path("."))
    classifier.add_argument("--windows-sandbox", action="store_true", help="Select only Windows sandbox execution paths")
    runner = subparsers.add_parser("run")
    runner.add_argument("--project", type=Path, required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--platform", required=True)
    runner.add_argument("--sha", required=True)
    runner.add_argument("--execution", choices=("serial", "parallel"), default="parallel")
    runner.add_argument("--suite", choices=("all", "common", "native"), default="all")
    runner.add_argument("tests", nargs="*", help="Explicit pytest files (default: configured testpaths)")
    summarizer = subparsers.add_parser("summarize")
    summarizer.add_argument("--reports", type=Path, required=True)
    summarizer.add_argument("--output", type=Path, required=True)
    summarizer.add_argument("--platforms", nargs="+", default=PLATFORMS)
    summarizer.add_argument("--native-platforms", nargs="+", default=[])
    summarizer.add_argument("--target", type=Path)
    summarizer.add_argument("--run-attempt", type=int, help="Select the latest attempt per platform from Actions artifacts")
    for job in ("resolve", "checks", "test", "native"):
        summarizer.add_argument(f"--{job}-result")
    args = parser.parse_args()
    if args.command == "resolve":
        return resolve()
    if args.command == "classify":
        return classify(args)
    if args.command == "run":
        return run(args)
    return summarize(args)


if __name__ == "__main__":
    sys.exit(main())
