"""Exercise revision resolution through the real CLI with an offline gh executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "compare_tests.py"


def main():
    with tempfile.TemporaryDirectory(prefix="virgo-resolve-check-") as directory:
        root = Path(directory)
        gh = root / "gh.py"
        gh.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "endpoint = sys.argv[2]\n"
            "with open('api_calls', 'a') as log: log.write(endpoint + '\\n')\n"
            "print(json.dumps(json.loads(Path('api.json').read_text())[endpoint]))\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            (root / "gh.cmd").write_text(f'@"{sys.executable}" "{gh}" %*\n', encoding="utf-8")
        else:
            launcher = root / "gh"
            launcher.write_text(f"#!{sys.executable}\n" + gh.read_text(encoding="utf-8"), encoding="utf-8")
            launcher.chmod(0o755)

        def pr(number, base, head, repository="contributor/fork"):
            return {
                "number": number, "html_url": f"https://github.com/owner/project/pull/{number}",
                "base": {"sha": base * 40, "repo": {"full_name": "owner/project"}},
                "head": {"sha": head * 40, "repo": {"full_name": repository}, "ref": "feature"},
            }

        original, latest = pr(10, "a", "b"), pr(10, "c", "d")
        endpoint = "repos/owner/project/pulls/10"
        listing = "repos/owner/project/pulls?state=open&sort=created&direction=desc&per_page=100"
        newest = pr(11, "e", "f")
        newest["head"]["ref"] = "codex/cross-platform-ci"
        newest_endpoint = "repos/owner/project/pulls/11"
        (root / "api.json").write_text(json.dumps({endpoint: latest, listing: [newest, latest],
                                                 newest_endpoint: newest}))

        def resolve(event_name, payload, number, expected, calls):
            for filename in ("target.json", "outputs", "api_calls"):
                (root / filename).unlink(missing_ok=True)
            event = root / "event.json"
            if payload is None:
                event.unlink(missing_ok=True)
            else:
                event.write_text(json.dumps(payload), encoding="utf-8")
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "GITHUB_REPOSITORY": "owner/project", "GITHUB_EVENT_NAME": event_name,
                   "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(root / "outputs"),
                   "GITHUB_SHA": payload.get("after", "") if payload else "", "PR_NUMBER": number}
            result = subprocess.run([sys.executable, str(RUNNER), "resolve"], cwd=root,
                                    env=env, text=True, capture_output=True, check=False)
            actual_calls = (root / "api_calls").read_text().splitlines() if (root / "api_calls").exists() else []
            assert actual_calls == calls, (event_name, actual_calls, calls)
            if expected is None:
                assert result.returncode != 0, result.stdout
                assert not (root / "target.json").exists()
                assert not (root / "outputs").exists()
                return
            assert result.returncode == 0, result.stderr
            target = json.loads((root / "target.json").read_text())
            if event_name == "push":
                wanted = {"pr": None, "base_sha": expected["before"], "head_sha": expected["after"],
                          "base_repository": "owner/project", "head_repository": "owner/project", "mode": "head"}
            else:
                wanted = {"pr": expected["number"], "url": expected["html_url"],
                          "base_sha": expected["base"]["sha"], "head_sha": expected["head"]["sha"],
                          "base_repository": expected["base"]["repo"]["full_name"],
                          "head_repository": expected["head"]["repo"]["full_name"], "mode": "comparison"}
            assert {key: target[key] for key in wanted} == wanted
            outputs = dict(line.split("=", 1) for line in (root / "outputs").read_text().splitlines())
            for key in ("base_sha", "head_sha", "base_repository", "head_repository", "mode"):
                assert outputs[key] == target[key]
            assert json.loads(outputs["revisions"]) == (["head"] if event_name == "push" else ["base", "head"])

        # The live API already points at B: queued/re-run event A must still resolve A.
        for repository in ("owner/project", "contributor/fork"):
            original["head"]["repo"]["full_name"] = repository
            for _ in range(2):
                resolve("pull_request", {"pull_request": original}, "10", original, [])
        # Event identity wins over a conflicting manual input.
        resolve("pull_request", {"pull_request": original}, "999", original, [])
        # Missing/malformed events must fail, never silently switch to live API data.
        for payload in (None, {}, {"pull_request": {}}):
            resolve("pull_request", payload, "10", None, [])
        resolve("workflow_dispatch", {}, "10", latest, [endpoint])
        resolve("workflow_dispatch", {}, "", newest, [listing, newest_endpoint])
        for invalid in ("invalid", "-1", "0", "1\n2", "１２"):
            resolve("workflow_dispatch", {}, invalid, None, [])
        push = {"before": "a" * 40, "after": "b" * 40}
        resolve("push", push, "999", push, [])
        initial = {"before": "0" * 40, "after": "b" * 40}
        resolve("push", initial, "", initial, [])
        for payload in ({}, {"before": "a" * 40, "after": "bad"}):
            resolve("push", payload, "", None, [])
        resolve("schedule", {}, "10", None, [])

        # Failure modes specified before implementation: code renamed into docs
        # being skipped; partial/unavailable Git history being assumed docs-only;
        # live PR/API drift changing an event's target; manual runs being skipped;
        # a root Markdown/config file accidentally broadening the docs allowlist.
        repo = root / "repo"
        repo.mkdir()

        def git(*arguments):
            return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip()

        git("init", "-q")
        git("config", "user.name", "CI acceptance")
        git("config", "user.email", "ci@example.invalid")
        git("config", "commit.gpgsign", "false")

        def commit(path, content):
            file = repo / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8")
            git("add", "--all")
            git("commit", "-qm", "acceptance fixture")
            return git("rev-parse", "HEAD")

        base = commit("README.md", "initial")
        docs = commit("docs/nested/guide.md", "documentation")
        code = commit("src/code.py", "print('hello')")
        (repo / "src/code.py").rename(repo / "docs/code.md")
        renamed = commit("README.md", "rename code into docs")
        config = commit("AGENTS.md", "runtime configuration")
        odd_name = commit("docs/two\nlines.md", "documentation") if os.name != "nt" else config

        def classify(before, after, expected, event="pull_request"):
            target = {"base_sha": before, "head_sha": after, "event": event, "mode": "comparison"}
            target_file = root / "target.json"
            target_file.write_text(json.dumps(target))
            outputs = root / "outputs"
            outputs.unlink(missing_ok=True)
            result = subprocess.run([sys.executable, str(RUNNER), "classify", "--target", str(target_file),
                                     "--project", str(repo)], env={**os.environ, "GITHUB_OUTPUT": str(outputs)},
                                    capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            classified = json.loads(target_file.read_text())
            assert classified["run_tests"] is expected, classified
            assert classified["skip_reason"], classified
            assert f"run_tests={str(expected).lower()}" in outputs.read_text()

        classify(base, docs, False)
        classify(base, docs, False, "push")
        classify(base, docs, True, "workflow_dispatch")
        classify(docs, code, True)
        classify(code, renamed, True)
        classify(renamed, config, True)
        classify("0" * 40, docs, True)
        classify("f" * 40, docs, True)
        classify(docs, docs, True)  # Empty change lists conservatively run tests.
        if os.name != "nt":
            classify(config, odd_name, False)
    print("Revision resolution CLI end-to-end checks passed")


if __name__ == "__main__":
    main()
