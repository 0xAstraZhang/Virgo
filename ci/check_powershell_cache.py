"""Exercise the CI runner with real concurrent PowerShell processes.

Failure modes specified before implementation: inherited corrupt startup cache;
concurrent profile writers; cache protection breaking shell startup/commands;
changed HOME or parent environment; leaked temporary directories after success,
test failure or collection error; swallowed pytest failure; different protection
for base and head; unintended Windows environment changes. Missing pwsh fails
this acceptance check rather than silently skipping it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

RUNNER = Path(__file__).with_name("compare_tests.py").resolve()


def main():
    powershell = shutil.which("pwsh")
    if powershell is None:
        raise SystemExit("PowerShell cache acceptance requires a real pwsh on PATH")
    with tempfile.TemporaryDirectory(prefix="virgo-cache-check-") as directory:
        root = Path(directory)
        inherited = root / "inherited-cache"
        profile = inherited / "powershell" / "StartupProfileData-NonInteractive"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b"original-cache-sentinel")
        temporary = root / "temporary"
        temporary.mkdir()
        project = root / "project"
        project.mkdir()
        source = project / "test_cache.py"
        cache_path = project / "cache-path.txt"
        env = {**os.environ, "XDG_CACHE_HOME": str(inherited),
               "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
               "VIRGO_TEST_PWSH": powershell, "VIRGO_INHERITED_CACHE": str(inherited),
               "VIRGO_EXPECTED_HOME": os.environ.get("HOME", "")}
        original_environment = dict(os.environ)
        source.write_text(textwrap.dedent('''\
            import os
            import subprocess
            from concurrent.futures import ThreadPoolExecutor
            from pathlib import Path

            def test_cache_and_concurrent_processes():
                cache = Path(os.environ["XDG_CACHE_HOME"])
                Path("cache-path.txt").write_text(str(cache), encoding="utf-8")
                assert os.environ.get("HOME", "") == os.environ["VIRGO_EXPECTED_HOME"]
                if os.name == "nt":
                    assert str(cache) == os.environ["VIRGO_INHERITED_CACHE"]
                else:
                    assert str(cache) != os.environ["VIRGO_INHERITED_CACHE"]
                    for name in ("Interactive", "NonInteractive"):
                        profile = cache / "powershell" / f"StartupProfileData-{name}"
                        assert profile.read_bytes() == b""
                        assert profile.stat().st_mode & 0o222 == 0
                        assert not os.access(profile, os.W_OK)
                    # Other caches must stay writable for actual command behavior.
                    (cache / "command-cache").write_text("writable")

                def launch(index):
                    command = ("$PSVersionTable.PSVersion.ToString(); "
                               f"Write-Output 'command-{index}'")
                    result = subprocess.run([
                        os.environ["VIRGO_TEST_PWSH"], "-NoLogo", "-NoProfile",
                        "-NonInteractive", "-Command", command,
                    ], capture_output=True, text=True, timeout=30)
                    assert result.returncode == 0, result.stderr
                    assert f"command-{index}" in result.stdout
                    assert result.stdout.splitlines()[0].startswith("7.")

                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(launch, range(24)))
                if os.name != "nt":
                    assert (cache / "powershell" / "StartupProfileData-NonInteractive").read_bytes() == b""
            '''), encoding="utf-8")

        def run(label, expected):
            cache_path.unlink(missing_ok=True)
            output = root / "reports" / label
            result = subprocess.run([
                sys.executable, str(RUNNER), "run", "--project", str(project),
                "--output", str(output), "--platform", "local", "--label", label,
                "--sha", label,
            ], env=env, capture_output=True, text=True, timeout=120)
            assert result.returncode == expected, result.stdout + result.stderr
            report = json.loads((output / "results.json").read_text(encoding="utf-8"))
            assert report["exit_code"] == expected
            if cache_path.exists() and os.name != "nt":
                assert not Path(cache_path.read_text(encoding="utf-8")).exists()
            assert not list(temporary.glob("virgo-powershell-*")), "startup cache leaked"
            assert profile.read_bytes() == b"original-cache-sentinel"
            assert dict(os.environ) == original_environment
            return report

        for label in ("base", "head"):
            assert run(label, 0)["counts"] == {"passed": 1}
        source.write_text(
            'import os\nfrom pathlib import Path\n'
            'def test_failure():\n'
            '    Path("cache-path.txt").write_text(os.environ["XDG_CACHE_HOME"], encoding="utf-8")\n'
            '    assert False, "intentional failure"\n', encoding="utf-8",
        )
        assert run("head", 1)["counts"] == {"failed": 1}
        source.write_text('raise RuntimeError("collection failed")\n', encoding="utf-8")
        assert not run("head", 2)["complete"]
    print("PowerShell cache CLI acceptance passed (48 real concurrent launches)")


if __name__ == "__main__":
    main()
