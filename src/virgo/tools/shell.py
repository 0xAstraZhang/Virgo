"""Discover supported interpreters and bind each command tool to one immutable shell."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..agent.types import AgentTool, ToolResult
from ..i18n import Translator

SUPPORTED = ("bash", "zsh", "sh", "dash", "pwsh", "powershell")
SHELL_TOOLS = {"bash", "powershell"}

_SANDBOX_WRITE_ERROR_RE = re.compile(
    r"operation not permitted|read-only file system|permission denied|access is denied"
    r"|unauthorizedaccessexception|failure writing output|failed writing (?:body|received data)"
    r"|failed to create the file|cannot create (?:regular file|directory)"
    r"|errno (?:1|13|30)\b|\b(?:eperm|eacces|erofs)\b",
    re.IGNORECASE,
)
_TMP_PATH_RE = re.compile(r"(?:^|[\s\"'=:(>])/(?:private/)?tmp(?:/|[\s\"'&;|):]|$)")


def _tmp_is_writable(write_paths: list[str] | tuple[str, ...]) -> bool:
    for raw in write_paths:
        normalized = raw.rstrip("/") or "/"
        if normalized in {"/", "/tmp", "/private/tmp"}:
            return True
    return False


def sandbox_failure_hint(
    command: str,
    output_text: str,
    boundary: dict | None,
    *,
    t=None,
) -> str | None:
    """Return a concise sandbox write-boundary hint when a sandboxed command hits a write denial."""
    if not isinstance(boundary, dict) or boundary.get("provider") != "sandbox":
        return None
    if not output_text or not _SANDBOX_WRITE_ERROR_RE.search(output_text):
        return None
    effective = (boundary.get("effective") or {}).get("native_process") or boundary.get("requested") or {}
    if effective.get("unrestricted"):
        return None
    t = t or Translator()
    write_paths = effective.get("write_paths") or []
    is_posix = boundary.get("backend") != "windows-appcontainer"
    if (
        is_posix
        and not _tmp_is_writable(write_paths)
        and (_TMP_PATH_RE.search(command or "") or _TMP_PATH_RE.search(output_text))
    ):
        return t("tools.output.sandbox_tmp_hint")
    if not write_paths:
        return t("tools.output.sandbox_readonly_hint" if is_posix else "tools.output.sandbox_windows_readonly_hint")
    return t("tools.output.sandbox_write_hint" if is_posix else "tools.output.sandbox_windows_write_hint")


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


@dataclass
class ShellTool(AgentTool):
    shell: Shell | None = None


def make_shell_tool(shell: Shell, jobs=None, *, execution=None, on_unavailable=None) -> ShellTool:
    from .bash import BASH_PARAMS, DEFAULT_TIMEOUT, MAX_TIMEOUT, format_output, run_command
    from .output import MAX_BYTES, MAX_LINES

    def _cancelled_result(error: Exception) -> ToolResult:
        return ToolResult.error(str(error), details={"status": "aborted", "cancelled": True,
                                  "boundary": {"kind": "aborted", "payload_started": False,
                                              "side_effects": "none", "escalation_available": False}})

    def _unavailable_result(error: Exception) -> ToolResult:
        reason = getattr(error, "reason", None)
        message = str(error)
        if (reason in {None, "backend_unavailable",
                       "windows_network_isolation_unavailable", "windows_null_device_unavailable",
                       "windows_runtime_inaccessible", "windows_unsupported_os"}
                and "--permission no-limit" not in message):
            message = (
                f"{message}\n"
                "To run without the native sandbox, switch with `/permissions no-limit` "
                "or start with `--permission no-limit`."
            )
        return ToolResult.error(message, details={"status": "execution_unavailable", "boundary": {
            "kind": "execution_unavailable", "dimension": getattr(error, "dimension", None),
            "target": getattr(error, "target", None), "reason": reason,
            "payload_started": False, "side_effects": "none", "escalation_available": False,
        }})

    def _failed_result(error: Exception) -> ToolResult:
        return ToolResult.error(str(error), details={"status": "execution_failed", "boundary": {
            "kind": "execution_failed", "payload_started": "unknown", "side_effects": "possible",
            "escalation_available": False, **getattr(error, "boundary", {}),
        }})

    def execute(args, ctx):
        from ..execution import ExecutionCancelled, ExecutionFailed, ExecutionUnavailable

        policy = None
        try:
            policy = execution(ctx) if execution is not None else None
            result = execute_with_policy(args, ctx, policy)
        except ExecutionCancelled as error:
            result = _cancelled_result(error)
        except ExecutionUnavailable as error:
            fallback = None
            if on_unavailable is not None and policy is not None and getattr(policy, "provider", None) != "host":
                fallback = on_unavailable(ctx, args, error)
            if fallback is not None:
                policy = fallback
                try:
                    result = execute_with_policy(args, ctx, fallback)
                    facts = (result.details or {}).get("boundary", {})
                    result.details = {**(result.details or {}), "boundary": {**facts, "fallback": "user_approved_no_limit_once"}}
                except ExecutionCancelled as inner:
                    result = _cancelled_result(inner)
                except ExecutionUnavailable as inner:
                    result = _unavailable_result(inner)
                except ExecutionFailed as inner:
                    result = _failed_result(inner)
            else:
                result = _unavailable_result(error)
        except ExecutionFailed as error:
            result = _failed_result(error)
        if policy is not None:
            facts = (result.details or {}).get("boundary", {})
            result.details = {**(result.details or {}), "boundary": {**policy.describe(), **facts}}
        return result

    def execute_with_policy(args, ctx, policy):
        timeout = min(float(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        if jobs is not None and args.get("background"):
            job = jobs.start_command(
                args["command"],
                ctx.cwd,
                timeout if args.get("timeout") else None,
                shell=shell,
                store=ctx.origin.root if ctx.origin is not None else None,
                agent=ctx.agent,
                tool_call_id=ctx.call_id,
                execution_context=policy, cancellation=ctx.signal,
            )
            return ToolResult.text(
                ctx.t("tools.shell.job_started", id=job.id, kind=shell.kind, version=shell.version),
                details={"job_id": job.id, "boundary": dict(job.boundary or {})},
            )
        out, code, status = run_command(args["command"], ctx.cwd, timeout, ctx.signal, interpreter=shell,
                                        **({"execution_context": policy} if policy is not None else {}))
        result = format_output(out, code, status, ctx.cwd, timeout, prefix=shell.tool_name, t=ctx.t)
        result.details.update(shell=shell.kind, executable=shell.executable, version=shell.version)
        if out.boundary is not None:
            result.details["boundary"] = out.boundary
        if status == "ok" and code:
            effective_boundary = {**(policy.describe() if policy is not None else {}), **(out.boundary or {})}
            hint = sandbox_failure_hint(args["command"], out.text, effective_boundary, t=ctx.t)
            if hint:
                result = ToolResult.error(f"{result.as_text()}\n{hint}", result.details)
        return result

    params = {**BASH_PARAMS, "properties": {**BASH_PARAMS["properties"]}}
    params["properties"]["timeout"] = {**params["properties"]["timeout"], "exclusiveMinimum": 0}
    if jobs is not None:
        params["properties"]["background"] = {
            "type": "boolean",
            "description": "Run in the background; job_output/job_kill manage the job.",
        }

    def values(t):
        error_key = "tools.shell.errors.posix"
        if shell.tool_name == "powershell":
            error_key = (
                "tools.shell.errors.powershell" if shell.native_errors else "tools.shell.errors.compatibility"
            )
        elif shell.kind in {"sh", "dash"}:
            error_key = "tools.shell.errors.sh"
        return {
            "syntax": shell.syntax,
            "environment": shell.environment(t),
            "errors": t(error_key),
            "max_lines": MAX_LINES,
            "max_kb": MAX_BYTES // 1024,
            "background": t("tools.shell.background") if jobs is not None else "",
        }

    translator = Translator()
    return ShellTool(
        execution_scope="process",
        name=shell.tool_name,
        label=shell.syntax,
        description=translator("tools.shell.description", **values(translator)),
        parameters=params,
        execute=execute,
        shell=shell,
        i18n_key="tools.shell",
        i18n_values=values,
    )
