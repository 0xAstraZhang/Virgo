"""Run pytest with a private, protected PowerShell startup cache on Unix."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    command = [sys.executable, "-m", "pytest", *sys.argv[1:]]
    if os.name == "nt":
        return subprocess.call(command)

    # Concurrent probes/commands can corrupt .NET's startup profile, even
    # within one pytest process. A private directory alone does not prevent it.
    # PowerShell/PowerShell#26528: keep only the two startup profiles read-only;
    # module analysis and caches used by commands retain a writable directory.
    with tempfile.TemporaryDirectory(prefix="virgo-powershell-") as directory:
        cache = Path(directory) / "powershell"
        cache.mkdir()
        for mode in ("Interactive", "NonInteractive"):
            profile = cache / f"StartupProfileData-{mode}"
            profile.touch()
            profile.chmod(0o444)
            if os.access(profile, os.W_OK):
                raise SystemExit("PowerShell cache protection requires an unprivileged CI user")
        # A fresh cache also triggers telemetry UUID creation. Concurrent pwsh
        # starts can crash in .NET's named-mutex wait before running a command.
        # Opt out before process startup; see docs/en/development/testing-and-ci.md#runner-maintenance.
        env = {**os.environ, "XDG_CACHE_HOME": directory,
               "POWERSHELL_TELEMETRY_OPTOUT": "true"}
        return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main())
