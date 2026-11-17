"""Pinned Windows MXC feasibility gate; never substitutes rejection time for work."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

BASELINE = "f33be1cae82d4e25006d88411aea68d9be9c8033"
MXC_VERSION = "v1.1.0"
ARCHIVE_SHA256 = "31a44963e288e3af23bc2e7e350608ed28f7c76b922ee7a7f1758a486dda37b3"
ARCHIVE_URL = f"https://github.com/microsoft/mxc/releases/download/{MXC_VERSION}/mxc-release-binaries.zip"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def invoke(argv, *, cwd=None, env=None, timeout=60):
    started = time.perf_counter()
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        return {"argv": argv, "exit_code": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "elapsed_seconds": time.perf_counter() - started}
    except subprocess.TimeoutExpired as error:
        return {"argv": argv, "exit_code": None, "timeout": True,
                "stdout": (error.stdout or b"").decode("utf-8", "replace"),
                "stderr": (error.stderr or b"").decode("utf-8", "replace"),
                "elapsed_seconds": time.perf_counter() - started}


def environment():
    return {"baseline_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "harness_sha": subprocess.check_output(
                ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"], text=True).strip(),
            "platform": platform.platform(), "windows_version": list(sys.getwindowsversion()),
            "python": sys.version, "python_executable": sys.executable,
            "runner_image": os.environ.get("ImageOS"), "runner_image_version": os.environ.get("ImageVersion"),
            "architecture": platform.machine(), "mxc_version": MXC_VERSION,
            "os": invoke(["pwsh", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture | ConvertTo-Json"]),
            "powershell": invoke(["pwsh", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"])}


def install_mxc(output):
    cache = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "virgo-mxc-v1.1.0"
    cache.mkdir(exist_ok=True)
    archive = cache / "release.zip"
    urllib.request.urlretrieve(ARCHIVE_URL, archive)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != ARCHIVE_SHA256:
        raise RuntimeError(f"MXC archive checksum mismatch: {digest}")
    with zipfile.ZipFile(archive) as bundle:
        inventory = [{"path": item.filename, "size": item.file_size} for item in bundle.infolist()]
        save(output / "release-inventory.json", inventory)
        candidates = []
        for item in bundle.infolist():
            if Path(item.filename).name.lower() != "wxc-exec.exe":
                continue
            with bundle.open(item) as stream:
                header = stream.read(64)
                if header[:2] != b"MZ":
                    continue
                stream.seek(struct.unpack_from("<I", header, 60)[0])
                signature = stream.read(6)
                if signature[:4] == b"PE\0\0" and struct.unpack_from("<H", signature, 4)[0] == 0x8664:
                    candidates.append(item.filename)
        if len(candidates) != 1:
            raise RuntimeError(f"Expected one x64 wxc-exec.exe, found {candidates}")
        executable = candidates[0]
        parent = Path(executable).parent
        destination = cache / "x64"
        destination.mkdir(exist_ok=True)
        # Keep adjacent native runtime files; do not install optional VM images.
        installed = []
        for item in bundle.infolist():
            name = Path(item.filename)
            if not item.is_dir() and name.parent == parent:
                target = destination / name.name
                with bundle.open(item) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                installed.append({"name": name.name, "bytes": item.file_size})
    launcher = destination / "wxc-exec.exe"
    save(output / "release.json", {"version": MXC_VERSION, "url": ARCHIVE_URL,
                                   "archive_sha256": digest, "archive_bytes": archive.stat().st_size,
                                   "selected_member": executable, "installed_files": installed,
                                   "executor_bytes": launcher.stat().st_size,
                                   "executor_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest()})
    return launcher


def probe(output):
    from virgo.tools.shell import Shell

    metadata = environment()
    save(output / "environment.json", metadata)
    if metadata["baseline_sha"] != BASELINE:
        raise RuntimeError("Evaluation must use the frozen baseline checkout")
    launcher = install_mxc(output)
    save(output / "host-probe.json", invoke([str(launcher), "--probe"]))
    root = Path(tempfile.mkdtemp(prefix="virgo-mxc-probe-"))
    project, scratch = root / "workspace", root / "scratch"
    project.mkdir()
    scratch.mkdir()
    private = root / "private.txt"
    private.write_text("private-canary", encoding="utf-8")
    pwsh = Path(shutil.which("pwsh"))
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "TZ"}}
    env.update(HOME=str(scratch), USERPROFILE=str(scratch), TEMP=str(scratch), TMP=str(scratch),
               POWERSHELL_TELEMETRY_OPTOUT="1", POWERSHELL_UPDATECHECK="Off", VIRGO="1")
    records = []
    # Direct Python is a minimal control; PowerShell + Python matches the E2E toolchain.
    for shell in (False, True):
        for read_only, network in ((False, False), (True, False), (False, True), (True, True)):
            label = f"{'pwsh-python' if shell else 'python'}-ro{int(read_only)}-net{int(network)}"
            marker = scratch / f"{label}.txt"
            source = f"from pathlib import Path;Path({str(marker)!r}).write_text('started');print('mxc-payload-started')"
            argv = [sys.executable, "-I", "-S", "-c", source]
            if shell:
                import base64

                encoded = base64.b64encode(source.encode()).decode()
                bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
                command = "& " + " ".join("'" + item.replace("'", "''") + "'" for item in
                                          [sys.executable, "-I", "-S", "-c", bootstrap])
                argv = Shell("pwsh", str(pwsh), "unknown").argv(command)
            control = invoke(argv, cwd=project, env=env)
            control["marker_created"] = marker.exists()
            marker.unlink(missing_ok=True)
            readonly = [sys.prefix, sys.base_prefix, str(pwsh.parent)]
            request = {
                "version": "1.0.0", "containment": "processcontainer",
                "process": {"commandLine": subprocess.list2cmdline(argv), "cwd": str(project),
                            "env": [f"{key}={value}" for key, value in env.items()],
                            "inheritDefaultEnv": False, "timeout": 15000},
                "filesystem": {"readonlyPaths": [*readonly, *([str(project)] if read_only else [])],
                               "readwritePaths": [str(scratch), *([] if read_only else [str(project)])],
                               "deniedPaths": [str(private)]},
                "processContainer": {"leastPrivilege": True},
                "network": {"egress": {"default": "allow" if network else "deny"},
                            "ingress": {"default": "deny", "hostLoopback": "deny"}},
                "fallback": {"allowDaclMutation": False},
            }
            config = output / f"request-{label}.json"
            save(config, request)
            capabilities = invoke([str(launcher), "--probe", str(config)])
            actual = invoke([str(launcher), str(config)], cwd=project, env=env)
            actual["marker_created"] = marker.exists()
            records.append({"case": label, "host_control": control, "probe": capabilities, "launch": actual})
            save(output / "mxc-startup.json", records)
    controls_ok = all(r["host_control"]["exit_code"] == 0 and r["host_control"]["marker_created"] for r in records)
    started = [r["case"] for r in records if r["launch"]["marker_created"]]
    timed_out = any(r["launch"].get("timeout") for r in records)
    status = "native-startup-observed-needs-adapter" if started else "blocked-no-native-startup"
    if not controls_ok or timed_out:
        status = "inconclusive-control-or-timeout"
    save(output / "gate.json", {"status": status, "host_controls_passed": controls_ok,
                                "started_cases": started, "sandbox_acceptance": False,
                                "note": "Startup is not boundary acceptance; rejection duration is not execution performance."})
    print(json.dumps({"status": status, "started_cases": started}))
    if not controls_ok or timed_out:
        raise RuntimeError("Startup experiment lacks reliable controls")


def benchmark(output):
    from virgo.tools.bash import run_command
    from virgo.tools.sandbox import SandboxPolicy
    from virgo.tools.shell import Shell

    project = Path(tempfile.mkdtemp(prefix="virgo-lpac-benchmark-"))
    shell = Shell("pwsh", shutil.which("pwsh"), "unknown")
    records = []
    for size in (0, 1000):
        for index in range(size):
            (project / f"file-{index:04}.txt").write_text("ordinary workspace file\n", encoding="utf-8")
        for mode in ("read-only", "workspace-write"):
            for sample in range(6):
                begin = time.perf_counter()
                try:
                    result, code, status = run_command("Write-Output 'benchmark-ok'", project, 30,
                                                      threading.Event(), interpreter=shell,
                                                      sandbox_policy=SandboxPolicy(mode, project))
                    record = {"exit_code": code, "status": status, "output": result.text}
                    success = code == 0 and status == "ok" and "benchmark-ok" in result.text
                except Exception as error:
                    record, success = {"error": f"{type(error).__name__}: {error}"}, False
                records.append({"backend": "windows-lpac", "workspace_files": size, "mode": mode,
                                "sample": sample, "first_in_group": sample == 0, "success": success,
                                "elapsed_seconds": time.perf_counter() - begin, **record})
                save(output / "baseline-timings.json", records)
                if not success:
                    break
    summary = []
    for size in (0, 1000):
        for mode in ("read-only", "workspace-write"):
            group = [r for r in records if r["workspace_files"] == size and r["mode"] == mode]
            warm = sorted(r["elapsed_seconds"] for r in group if r["success"] and not r["first_in_group"])
            summary.append({"workspace_files": size, "mode": mode, "samples": len(group),
                            "failures": sum(not r["success"] for r in group),
                            "first_seconds": group[0]["elapsed_seconds"] if group[0]["success"] else None,
                            "warm_median_seconds": statistics.median(warm) if warm else None,
                            "warm_p95_seconds": warm[math.ceil(len(warm) * .95) - 1] if warm else None})
    save(output / "baseline-timing-summary.json", {"groups": summary, "peak_memory_bytes": None,
                                                   "note": "Tiny serial sample; no paired MXC speed or memory claim."})
    if any(not r["success"] for r in records):
        raise RuntimeError("Baseline timing contains unsuccessful commands")


def summarize(output):
    report = ET.parse(output / "baseline-junit.xml")
    cases = report.findall(".//testcase")
    counts = {"executed": len(cases), **{kind: len(report.findall(f".//testcase/{kind}"))
                                       for kind in ("failure", "error", "skipped")}}
    gate = json.loads((output / "gate.json").read_text(encoding="utf-8"))
    save(output / "summary.json", {"baseline": counts, "mxc": gate,
                                   "replacement_accepted": False, "product_lines_removed": 0})
    text = (f"## MXC {MXC_VERSION} evaluation\n\nBaseline: `{BASELINE}`\n\n"
            f"Existing E2E: `{json.dumps(counts)}`\n\nMXC gate: `{gate['status']}`\n\n"
            "No replacement acceptance or speedup is inferred from an unsupported host.\n")
    (output / "summary.md").write_text(text, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)
    print(text)
    if not cases or any(counts[kind] for kind in ("failure", "error", "skipped")):
        raise RuntimeError("Baseline end-to-end acceptance is not green")
    if gate["status"] != "native-startup-observed-needs-adapter":
        raise RuntimeError("MXC replacement is blocked; see raw native startup evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("probe", "benchmark", "summarize"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        raise SystemExit("Native Windows required; skipping is not validation")
    {"probe": probe, "benchmark": benchmark, "summarize": summarize}[args.phase](args.output)
