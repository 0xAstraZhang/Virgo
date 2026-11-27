"""Resolve immutable event revisions and classify doc-only changes."""

from __future__ import annotations

import json
import os
import re
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from .io import outputs, read_json, write_json

# Keep aligned with the Windows workflow's main-push filter. PRs use this
# decision inside the workflow so unrelated changes still complete its gate.
WINDOWS_SANDBOX_PATHS = (
    "src/virgo/**", "tests/conftest.py", "tests/support/**",
    "tests/harness/test_windows_sandbox_e2e.py", "ci/check_native_report.py",
    "ci/prepare_windows_null_device.py", "ci/run_as_standard_user.py",
    "ci/compare_tests.py", "ci/pipeline/**", ".github/workflows/windows-sandbox.yml",
    "pyproject.toml", "uv.lock",
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
            "pr": None,
            "url": f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/commit/{event['after']}",
            "base_sha": event["before"],
            "head_sha": event["after"],
            "base_repository": repository,
            "head_repository": repository,
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
                raise SystemExit("No open PR to test; supply a PR number.")
            number = str(candidates[0]["number"])
        if not re.fullmatch(r"[1-9][0-9]*", number):
            raise SystemExit("PR number must be a positive integer")
        pr = api(f"repos/{repository}/pulls/{number}")
    else:
        raise ValueError(f"Unsupported event: {event_name}")

    if event_name != "push":
        target = {
            "pr": int(number),
            "url": pr["html_url"],
            "base_sha": pr["base"]["sha"],
            "head_sha": pr["head"]["sha"],
            "base_repository": pr["base"]["repo"]["full_name"],
            "head_repository": pr["head"]["repo"]["full_name"],
        }

    for label in ("base", "head"):
        sha, repo = target[f"{label}_sha"], target[f"{label}_repository"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or (sha == "0" * 40 and (label != "base" or event_name != "push")):
            raise ValueError(f"Invalid {label} SHA")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"Invalid {label} repository")

    target["event"] = event_name
    write_json(Path("target.json"), target)
    outputs(target)
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
                if args.windows_sandbox:
                    run_tests = any(fnmatchcase(path, pattern) for path in paths for pattern in WINDOWS_SANDBOX_PATHS)
                    reason = "Windows sandbox execution paths changed" if run_tests else "No Windows sandbox execution paths changed"
                else:
                    run_tests = not all(
                        path in {"README.md", "README.zh-CN.md", "examples/README.md", "examples/README.zh-CN.md"}
                        or (path.startswith("docs/") and path.endswith(".md"))
                        for path in paths
                    )
                    reason = "Code or configuration changed" if run_tests else "Only documentation changed"
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
    target.update(run_tests=run_tests, skip_reason=reason)
    write_json(args.target, target)
    outputs({"run_tests": run_tests, "skip_reason": reason})
    print(reason)
