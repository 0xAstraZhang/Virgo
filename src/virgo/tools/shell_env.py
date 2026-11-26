"""Supported shell interpreter discovery, probing, and environment normalization."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..i18n import Translator

SUPPORTED = ("bash", "zsh", "sh", "dash", "pwsh", "powershell")
SHELL_TOOLS = {"bash", "powershell"}


@dataclass(frozen=True)
class Shell:
    kind: str
    executable: str
    version: str

    @property
    def tool_name(self) -> str:
        return "powershell" if self.kind in {"pwsh", "powershell"} else "bash"

    @property
    def native_errors(self) -> bool:
        match = re.match(r"(\d+)\.(\d+)", self.version)
        return self.tool_name == "powershell" and bool(match and tuple(map(int, match.groups())) >= (7, 4))

    @property
    def syntax(self) -> str:
        return (
            "PowerShell"
            if self.tool_name == "powershell"
            else {"bash": "Bash", "zsh": "Zsh"}.get(self.kind, "POSIX sh")
        )

    @property
    def label(self) -> str:
        return f"{self.kind} {self.version} — {self.executable}"

    def environment(self, t=None) -> str:
        t = t or Translator()
        return t(
            "tools.shell.environment",
            tool=self.tool_name,
            kind=self.kind,
            version=self.version,
            executable=self.executable,
            syntax=self.syntax,
        )

    def argv(self, command: str) -> list[str]:
        if self.tool_name == "powershell":
            # The payload is code; encoding avoids an intermediate CMD/Unix shell and quoting layer.
            # Explicit exit remains authoritative. Older engines only propagate the last native exit.
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                "$ProgressPreference = 'SilentlyContinue'\n"
                "[Console]::InputEncoding = [Console]::OutputEncoding = $OutputEncoding = "
                "New-Object System.Text.UTF8Encoding $false\n"
                "$PSNativeCommandUseErrorActionPreference = $true\n"
                "$LASTEXITCODE = 0\n"
                "try {\n" + command + "\n"
                "if (-not $?) { exit 1 }\n"
                "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
                "} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }\n"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            argv = [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-OutputFormat",
                "Text",
                "-EncodedCommand",
                encoded,
            ]
            if os.name == "nt" and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
                raise ValueError(
                    "PowerShell command is too long; write a .ps1 file and invoke it with & 'path'."
                )
            return argv
        options = {
            "bash": ["--noprofile", "--norc", "-e", "-o", "pipefail"],
            "zsh": ["-f", "-e", "-o", "pipefail"],
        }.get(self.kind, ["-e"])
        return [self.executable, *options, "-c", command]


def shell_environment() -> dict[str, str]:
    env = dict(os.environ, VIRGO="1")
    # Noninteractive POSIX shells can otherwise load startup files despite --noprofile.
    for key in ("BASH_ENV", "ENV"):
        env.pop(key, None)
    return env


def probe_shell(value: str, *, t=None, execute: bool = True) -> Shell:
    """Validate an exact supported path, optionally executing its version probe."""
    t = t or Translator()
    value = os.path.expanduser(value)
    kind = Path(value).name.lower().removesuffix(".exe")
    if kind not in SUPPORTED:
        raise ValueError(t("tools.shell.unsupported", value=value, supported=", ".join(SUPPORTED)))
    executable = shutil.which(value)
    if not executable:
        raise ValueError(t("tools.shell.unavailable", value=value))
    # Preserve invocation paths: /bin/sh can be a Bash symlink with different semantics.
    executable = os.path.abspath(executable)
    if (
        os.name == "nt"
        and kind == "bash"
        and Path(executable).parent.name.lower() in {"system32", "sysnative"}
    ):
        raise ValueError(t("tools.shell.wsl"))
    if not execute:
        # Discovery is broker work. An active sandbox must not execute a
        # project/PATH candidate merely to learn its version before isolation.
        return Shell(kind, executable, "unknown")
    if kind in {"pwsh", "powershell"}:
        args = [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ]
    elif kind == "bash":
        args = ["--noprofile", "--norc", "-c", 'printf "%s" "$BASH_VERSION"']
    elif kind == "zsh":
        args = ["-f", "-c", 'printf "%s" "$ZSH_VERSION"']
    else:
        args = ["-c", 'printf "%s" "${BASH_VERSION:-POSIX (version not reported)}"']
    try:
        result = subprocess.run(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=shell_environment(),
            timeout=3,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            t("tools.shell.probe_failed_error", executable=executable, type=type(error).__name__)
        ) from error
    version = result.stdout.decode("utf-8", errors="replace").strip()
    reason = ""
    if result.returncode or not version or len(version) > 120 or "\n" in version:
        reason = t("tools.shell.version_probe_failed")
    elif kind not in {"sh", "dash"} and not re.match(r"\d+\.\d+", version):
        reason = t("tools.shell.unknown_version")
    if reason:

        def excerpt(data: bytes) -> str:
            text = data.decode("utf-8", errors="replace")
            return repr(text[:1024]) + (t("tools.shell.probe_truncated") if len(text) > 1024 else "")

        raise ValueError(
            t(
                "tools.shell.probe_failed_details",
                executable=executable,
                reason=reason,
                code=result.returncode,
                stdout=excerpt(result.stdout),
                stderr=excerpt(result.stderr),
            )
        )
    if kind == "powershell" and not version.startswith("5.1.") and int(version.split(".")[0]) < 7:
        raise ValueError(t("tools.shell.powershell_version"))
    if kind == "pwsh" and int(version.split(".")[0]) < 7:
        raise ValueError(t("tools.shell.pwsh_version"))
    return Shell(kind, executable, version)


def discover_shells(extra: str | None = None, *, execute: bool = True) -> list[Shell]:
    candidates: list[str] = [extra] if extra else []
    folders = [p for p in os.get_exec_path() if p and Path(p).is_absolute()]
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        folders.append(str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0"))
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if directory := os.environ.get(key):
                folders.extend(str(p) for p in (Path(directory) / "PowerShell").glob("*") if p.is_dir())
                folders.append(str(Path(directory) / "Git" / "bin"))
    else:
        folders.extend(["/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"])
        try:
            candidates.extend(
                line.strip()
                for line in Path("/etc/shells").read_text().splitlines()
                if line.startswith("/") and Path(line.strip()).name in SUPPORTED
            )
        except OSError:
            pass
    for name in SUPPORTED:
        for folder in dict.fromkeys(folders):
            candidate = Path(folder) / (name + ".exe" if os.name == "nt" else name)
            # WSL can inherit PATH entries that the current user cannot traverse.
            if os.path.isfile(candidate):
                candidates.append(str(candidate))
    candidates = list(dict.fromkeys(candidates))

    def probe(candidate):
        try:
            return probe_shell(candidate, execute=execute)
        except ValueError:
            return None

    # Bounded probes run in parallel, so a broken interpreter cannot stall the picker indefinitely.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, candidates))
    found: list[Shell] = []
    seen = set()
    for shell in results:
        if shell is None:
            continue
        key = (shell.kind, os.path.normcase(os.path.realpath(shell.executable)))
        if key not in seen:
            seen.add(key)
            found.append(shell)
    return found


def default_shell(found: list[Shell] | tuple[Shell, ...], *, t=None) -> Shell:
    """Choose from an existing catalog without probing the machine again."""
    t = t or Translator()
    preferred = ("pwsh", "powershell", "bash", "sh") if os.name == "nt" else ("bash", "zsh", "sh")
    for name in preferred:
        if shell := next((s for s in found if s.kind == name), None):
            return shell
    raise ValueError(t("tools.shell.none_available"))
