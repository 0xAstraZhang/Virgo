"""Execute the suite and normalize JUnit outcomes and failure diagnostics."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .io import write_json
from .reports import PARAMETER_ALIASES


def run(args):
    project, output = args.project.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    junit = output / "junit.xml"
    junit.unlink(missing_ok=True)
    (output / "results.json").unlink(missing_ok=True)
    started = time.monotonic()
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen([
            sys.executable, "-u", str(Path(__file__).resolve().parents[1] / "run_suite.py"),
            "--execution", args.execution, "--output", str(output),
        ], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}) as result:
            for line in result.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            result.wait()
    report = {
        "platform": args.platform, "label": args.label, "sha": args.sha,
        "python": sys.version, "system": platform.platform(),
        "exit_code": result.returncode, "duration": round(time.monotonic() - started, 2),
        "complete": result.returncode in {0, 1}, "counts": {}, "tests": {},
        "collection_skips": [], "problems": [],
    }
    counts = Counter()
    failures = ["# Test failure diagnostics", ""]
    try:
        for case in ET.parse(junit).iter("testcase"):
            name = case.attrib["name"]
            status = next((tag for tag in ("error", "failure", "skipped") if case.find(tag) is not None), "passed")
            status = "failed" if status == "failure" else status
            if status in {"failed", "error"}:
                detail = case.find("failure") if status == "failed" else case.find("error")
                location = case.get("file", case.get("classname", ""))
                line = int(case.get("line", "0")) + 1
                failures.extend([f"## {name}", f"Location: {location}:{line}", "",
                                 "```text", detail.text or detail.get("message", ""), "```", ""])
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
    (output / "failures.md").write_text("\n".join(failures), encoding="utf-8")
    return result.returncode if result.returncode else int(bool(report["problems"]))
