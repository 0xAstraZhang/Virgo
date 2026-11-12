"""Exercise revision resolution through the real CLI with an offline gh executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).with_name("compare_tests.py").resolve()


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
        excluded = pr(11, "e", "f")
        excluded["head"]["ref"] = "codex/cross-platform-ci"
        (root / "api.json").write_text(json.dumps({endpoint: latest, listing: [excluded, latest]}))

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
                   "PR_NUMBER": number}
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
            target = {"pr": expected["number"], "url": expected["html_url"],
                      "base_sha": expected["base"]["sha"], "head_sha": expected["head"]["sha"],
                      "base_repository": expected["base"]["repo"]["full_name"],
                      "head_repository": expected["head"]["repo"]["full_name"]}
            assert json.loads((root / "target.json").read_text()) == target
            assert dict(line.split("=", 1) for line in (root / "outputs").read_text().splitlines()) == {
                key: str(value) for key, value in target.items()
            }

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
        resolve("workflow_dispatch", {}, "", latest, [listing, endpoint])
        resolve("workflow_dispatch", {}, "invalid", None, [])
    print("Revision resolution CLI end-to-end checks passed")


if __name__ == "__main__":
    main()
