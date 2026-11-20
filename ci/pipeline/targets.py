"""Resolve immutable event revisions, classify changed paths, and plan cached baselines."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .io import outputs, read_json, write_json
from .reports import validate_report

CI_FINGERPRINT_FILES = (
    ".github/workflows/pr-platform-comparison.yml",
    "ci/compare_tests.py",
    "ci/run_suite.py",
    "ci/run_pytest.py",
    "ci/parallel-files.json",
)


def ci_fingerprint(root: Path | None = None) -> str:
    """Hash CI orchestration and runner files so CI edits invalidate cached baselines."""
    base = (root or Path(__file__).resolve().parents[2]).resolve()
    digest = hashlib.sha256()
    paths = [base / rel for rel in CI_FINGERPRINT_FILES]
    pipeline_dir = base / "ci" / "pipeline"
    if pipeline_dir.is_dir():
        paths.extend(sorted(pipeline_dir.glob("*.py")))
    for path in paths:
        rel = path.relative_to(base).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _is_green_baseline(report: object, *, platform: str, sha: str) -> bool:
    try:
        validate_report(report)
    except (ValueError, TypeError):
        return False
    return (
        report["platform"] == platform
        and report["sha"] == sha
        and report["complete"] is True
        and report["exit_code"] == 0
        and not report["problems"]
    )


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
    fingerprint = ci_fingerprint()
    can_reuse_base = event_name == "pull_request" and target["base_repository"] == repository
    target.update(
        event=event_name,
        mode="head" if event_name == "push" else "comparison",
        ci_fingerprint=fingerprint,
        can_reuse_base=can_reuse_base,
    )
    write_json(Path("target.json"), target)
    outputs({
        **target,
        "revisions": ["head"] if target["mode"] == "head" else ["base", "head"],
    })
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
                run_tests = not all(path in {"README.md", "README.zh-CN.md"} or (path.startswith("docs/") and path.endswith(".md"))
                                    for path in paths)
                reason = "Code or configuration changed" if run_tests else "Only documentation changed"
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
    target.update(run_tests=run_tests, skip_reason=reason)
    write_json(args.target, target)
    outputs({"run_tests": run_tests, "skip_reason": reason})
    print(reason)


def plan_matrix(args):
    target = read_json(args.target)
    reused = {f"reused_{os_name.split('-', 1)[0]}": False for os_name in args.platforms}
    if target.get("run_tests") is False:
        outputs({**reused, "matrix": []})
        print("Skipping test matrix: tests not required.")
        return 0
    entries: list[dict[str, str]] = []
    can_reuse = bool(target.get("can_reuse_base")) and target.get("mode") == "comparison"
    for os_name in args.platforms:
        short = os_name.split("-", 1)[0]
        if target.get("mode") == "comparison":
            hit = False
            if can_reuse:
                cached = args.cache_dir / os_name / "results.json"
                if cached.is_file():
                    try:
                        data = read_json(cached)
                    except (OSError, ValueError, TypeError):
                        data = None
                    if _is_green_baseline(data, platform=os_name, sha=target["base_sha"]):
                        reused_report = {**data, "label": "base"}
                        validate_report(reused_report)
                        write_json(args.reused_dir / os_name / "results.json", reused_report)
                        hit = True
            reused[f"reused_{short}"] = hit
            if hit:
                print(f"Reusing cached green baseline for {os_name} ({target['base_sha'][:12]})")
            else:
                entries.append({"os": os_name, "revision": "base"})
        entries.append({"os": os_name, "revision": "head"})
    outputs({**reused, "matrix": entries})
    print(f"Scheduled {len(entries)} test job(s): " + ", ".join(f"{e['os']}/{e['revision']}" for e in entries))
    return 0


def stage_cache(args):
    eligible = False
    if args.report.is_file():
        try:
            data = read_json(args.report)
        except (OSError, ValueError, TypeError):
            data = None
        if _is_green_baseline(data, platform=args.platform, sha=args.sha):
            write_json(args.destination, data)
            eligible = True
    outputs({"eligible": eligible})
    print("Baseline cache eligible" if eligible else "Baseline cache skipped (not a green complete report)")
    return 0
