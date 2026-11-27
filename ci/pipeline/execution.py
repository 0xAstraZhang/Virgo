"""Execute the pytest suite directly across balanced workers and normalize JUnit results."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .io import write_json

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "__pycache__", "build", "dist", "node_modules",
}
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_WORKERS_WIN32 = 3
MANIFEST = Path(__file__).resolve().parents[1] / "parallel-files.json"


def _default_workers() -> int:
    raw = os.environ.get("VIRGO_CI_WORKERS", "").strip()
    if raw:
        value = int(raw)
        if value < 1:
            raise ValueError("VIRGO_CI_WORKERS must be >= 1")
        return value
    cap = DEFAULT_MAX_WORKERS_WIN32 if sys.platform == "win32" else DEFAULT_MAX_WORKERS
    return max(1, min(os.cpu_count() or 2, cap))


def discover_test_files(project: Path) -> list[str]:
    roots = [project]
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        try:
            config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            testpaths = config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
            if isinstance(testpaths, list) and testpaths:
                existing = [project / p for p in testpaths if (project / p).exists()]
                if existing:
                    roots = existing
        except (OSError, tomllib.TOMLDecodeError):
            pass
    found: set[str] = set()
    for root in roots:
        if root.is_file():
            found.add(root.relative_to(project).as_posix())
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                    found.add((Path(dirpath) / name).relative_to(project).as_posix())
    return sorted(found)


def load_schedule(suite: str) -> tuple[set[str], dict[str, float]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    serial_list = manifest.get("serial", [])
    if len(serial_list) != len(set(serial_list)):
        raise ValueError("Duplicate files in serial manifest")
    weights_by_platform = manifest.get("weights", {})
    weights: dict[str, float] = {k: float(v) for k, v in weights_by_platform.get("default", {}).items()}
    weights.update({k: float(v) for k, v in weights_by_platform.get(sys.platform, {}).items()})
    if suite == "common":
        weights.update({k: float(v) for k, v in manifest.get("common_weights", {}).items()})
    return set(serial_list), weights


def partition_lpt(files: list[str], weights: dict[str, float], worker_count: int) -> list[list[str]]:
    count = max(1, min(worker_count, len(files)))
    buckets: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for path in sorted(files, key=lambda item: (-weights.get(item, 1.0), item)):
        index = min(range(count), key=lambda i: (loads[i], len(buckets[i]), i))
        buckets[index].append(path)
        loads[index] += weights.get(path, 1.0)
    return [bucket for bucket in buckets if bucket]


@contextmanager
def _pytest_environment():
    """Isolate PowerShell's Unix startup profile cache and disable telemetry per worker."""
    base = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if os.name == "nt":
        yield base
        return
    # PowerShell/PowerShell#26528: keep only the two startup profiles read-only;
    # module analysis and caches used by commands retain a writable directory.
    # Also opt out of telemetry so concurrent starts do not contend on telemetry.uuid.
    with tempfile.TemporaryDirectory(prefix="virgo-powershell-") as directory:
        cache = Path(directory) / "powershell"
        cache.mkdir()
        for mode in ("Interactive", "NonInteractive"):
            profile = cache / f"StartupProfileData-{mode}"
            profile.touch()
            profile.chmod(0o444)
            if os.access(profile, os.W_OK):
                raise SystemExit("PowerShell cache protection requires an unprivileged CI user")
        yield {**base, "XDG_CACHE_HOME": directory, "POWERSHELL_TELEMETRY_OPTOUT": "true"}


def run(args):
    project, output = args.project.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    junit = output / "junit.xml"
    junit.unlink(missing_ok=True)
    (output / "results.json").unlink(missing_ok=True)
    workers = _default_workers()
    started = time.monotonic()
    io_lock = threading.Lock()

    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        def run_shard(label: str, selection: list[str]) -> tuple[int, Path]:
            xml = output / f"{label}.xml"
            xml.unlink(missing_ok=True)
            command = [
                sys.executable, "-m", "pytest",
                "-v", "-ra", "--tb=long", "--color=no", "--durations=30",
                "-o", "junit_family=legacy", f"--junitxml={xml}", *selection,
            ]
            if args.suite != "all":
                command.extend(["-m", "native" if args.suite == "native" else "not native"])
            with _pytest_environment() as env:
                if args.suite == "native":
                    env["VIRGO_REQUIRE_SANDBOX_TESTS"] = "1"
                with subprocess.Popen(
                    command, cwd=project, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                ) as process:
                    for line in process.stdout:
                        formatted = f"[{label}] {line}"
                        with io_lock:
                            log.write(formatted)
                            log.flush()
                            print(formatted, end="", flush=True)
                    return process.wait(), xml

        discovered = (args.tests or discover_test_files(project)) if args.execution == "parallel" else []
        if args.execution == "parallel" and len(discovered) > 1 and workers > 1:
            serial_set, weights = load_schedule(args.suite)
            parallel_files = [p for p in discovered if p not in serial_set]
            serial_files = [p for p in discovered if p in serial_set]
            results: list[tuple[int, Path]] = []
            if parallel_files:
                buckets = partition_lpt(parallel_files, weights, workers)
                if len(buckets) > 1:
                    with ThreadPoolExecutor(max_workers=len(buckets)) as pool:
                        futures = [pool.submit(run_shard, f"parallel-{i}", group) for i, group in enumerate(buckets)]
                        results.extend(f.result() for f in futures)
                else:
                    results.append(run_shard("parallel-0", buckets[0]))
            if serial_files:
                results.append(run_shard("serial", serial_files))
        else:
            results = [run_shard("serial", args.tests)]

    root = ET.Element("testsuites")
    codes: list[int] = []
    problems: list[str] = []
    for code, xml in results:
        codes.append(code)
        if not xml.exists():
            problems.append(f"Missing JUnit report: {xml.name}")
            codes.append(2)
            continue
        try:
            root.extend(ET.parse(xml).getroot())
        except (OSError, ET.ParseError) as error:
            problems.append(f"Cannot read JUnit report {xml.name}: {error}")
            codes.append(2)
    ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)

    if any(code not in (0, 1, 5) for code in codes):
        exit_code = 2
    elif 1 in codes:
        exit_code = 1
    else:
        exit_code = 0 if list(root.iter("testcase")) else 5

    report = {
        "platform": args.platform, "sha": args.sha, "suite": args.suite,
        "python": sys.version, "system": platform.platform(),
        "exit_code": exit_code, "duration": round(time.monotonic() - started, 2),
        "complete": exit_code in {0, 1}, "counts": {}, "tests": {},
        "collection_skips": [], "problems": problems,
    }
    counts = Counter()
    failures = ["# Test failure diagnostics", ""]
    for case in root.iter("testcase"):
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
        classname = case.get("classname", "")
        key = f"{classname}::{name}" if classname else name
        if not name.startswith("test_"):
            if status == "skipped":
                report["collection_skips"].append(key)
                continue
            report["problems"].append(f"Collection problem: {key}")
        if key in report["tests"]:
            report["problems"].append(f"Ambiguous test name: {key}")
        report["tests"][key] = {"status": status, "classname": classname}
    if not counts:
        report["problems"].append("No test outcomes in JUnit report")
    if not report["complete"]:
        report["problems"].append(f"pytest did not complete normally (exit {exit_code})")
    report["counts"] = dict(counts)
    if args.suite == "native" and (counts.get("passed", 0) == 0 or any(counts.get(status) for status in ("failed", "error", "skipped"))):
        report["problems"].append("Native acceptance requires executed cases with zero failures, errors and skips")
    write_json(output / "results.json", report)
    (output / "failures.md").write_text("\n".join(failures), encoding="utf-8")
    return exit_code if exit_code else int(bool(report["problems"]))
