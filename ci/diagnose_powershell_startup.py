import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

mode = sys.argv[1]
count = int(sys.argv[2])
root = Path(tempfile.mkdtemp(prefix="virgo-pwsh-diagnostic-"))
Path("diagnostic-path.txt").write_text(str(root))
cache = root / "cache" / "powershell"
cache.mkdir(parents=True)
profiles = [cache / f"StartupProfileData-{m}" for m in ("Interactive", "NonInteractive")]
if mode == "readonly":
    for profile in profiles:
        profile.touch()
        profile.chmod(0o444)
env = {
    **os.environ, "XDG_CACHE_HOME": str(cache.parent),
    "DOTNET_DbgEnableMiniDump": "1", "DOTNET_DbgMiniDumpType": "1",
    "DOTNET_DbgMiniDumpName": str(root / "pwsh.%p.dmp"),
    "DOTNET_EnableCrashReport": "1",
}
if mode == "disabled":
    env["DOTNET_MultiCoreJitMinNumCpus"] = "ffffffff"

def launch(index):
    process_env = env
    if mode == "private":
        process_env = {**env, "XDG_CACHE_HOME": str(root / f"cache-{index}")}
    result = subprocess.run([
        shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive",
        "-Command", f"$PSVersionTable.PSVersion.ToString(); Write-Output 'command-{index}'",
    ], env=process_env, capture_output=True, text=True, timeout=60)
    return {"index": index, "returncode": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr}

print(json.dumps({"mode": mode, "root": str(root), "count": count}), flush=True)
start = time.monotonic()
failures = []
completed = 0
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    for batch in range(0, count, 24):
        for result in pool.map(launch, range(batch, min(batch + 24, count))):
            completed += 1
            if result["returncode"] or f'command-{result["index"]}' not in result["stdout"]:
                failures.append(result)
                print(json.dumps(result), flush=True)
        if failures:
            break
        if completed % 120 == 0:
            print(json.dumps({"completed": completed, "seconds": time.monotonic() - start}), flush=True)
summary = {"mode": mode, "completed": completed, "failures": failures,
           "seconds": time.monotonic() - start,
           "profiles": {str(p): {"bytes": p.stat().st_size, "mode": oct(p.stat().st_mode)}
                        for p in profiles if p.exists()}}
(root / "result.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary), flush=True)
sys.exit(bool(failures))
