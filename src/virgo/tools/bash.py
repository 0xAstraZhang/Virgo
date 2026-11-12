"""bash: run a shell command with timeout, abort, and bounded output."""

from __future__ import annotations

import os
import signal as _signal
import subprocess
import threading
import time
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from .truncate import MAX_BYTES, MAX_LINES, save_output, truncate_tail

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
SHELL = os.environ.get("VIRGO_SHELL") or ("bash" if os.name != "nt" else None)


def spawn(command: str, cwd: Path, stdout=subprocess.PIPE, *, interpreter=None) -> subprocess.Popen:
    from .shell import shell_environment

    env = shell_environment()
    if interpreter is not None:
        argv, shell = interpreter.argv(command), False
    elif SHELL:
        # Do not let a later ls/head/cat hide a failed download or pipeline.
        # Only add flags for known shells; VIRGO_SHELL may be another interpreter.
        options = ["-e", "-o", "pipefail"] if Path(SHELL).name in {"bash", "zsh"} else []
        argv: list[str] | str = [SHELL, *options, "-c", command]
        shell = False
    else:
        argv, shell = command, True
    return subprocess.Popen(
        argv,
        shell=shell,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, _signal.SIGTERM)
            for _ in range(20):
                if proc.poll() is not None:
                    return
                time.sleep(0.05)
            os.killpg(proc.pid, _signal.SIGKILL)
        else:
            # Terminate descendants too (build tools and servers often spawn children).
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            try:
                subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            finally:
                if proc.poll() is None:
                    proc.kill()
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_command(
    command: str, cwd: Path, timeout: float, signal: threading.Event, *, interpreter=None
) -> tuple[str, int | None, str]:
    """Run to completion. Returns (output, exit_code, status) with status in ok|timeout|aborted."""
    if signal.is_set():
        return "", None, "aborted"
    proc = spawn(command, cwd, interpreter=interpreter)
    chunks: list[bytes] = []

    def pump():
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(8192), b""):
            chunks.append(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    status = "ok"
    while proc.poll() is None:
        if signal.is_set():
            status = "aborted"
            kill_tree(proc)
            break
        if time.monotonic() > deadline:
            status = "timeout"
            kill_tree(proc)
            break
        time.sleep(0.05)
    proc.wait(timeout=5)
    reader.join(timeout=2)
    out = b"".join(chunks).decode("utf-8", errors="replace")
    return out, proc.poll(), status


def format_output(output: str, code: int | None, status: str, cwd: Path, timeout: float, *, prefix="bash") -> ToolResult:
    text, cut = truncate_tail(output)
    if cut:
        path = save_output(output, cwd, prefix)
        text = (
            f"[Output truncated: showing the last {len(text.splitlines())} of {len(output.splitlines())} lines "
            f"(limit {MAX_LINES} lines / {MAX_BYTES // 1024}KB). Full output: {path}]\n{text}"
        )
    text = text.rstrip() or "(no output)"
    details = {"exit_code": code, "status": status}
    if status == "timeout":
        return ToolResult.error(f"{text}\n\n[Command timed out after {timeout:g}s]", details)
    if status == "aborted":
        return ToolResult.error(f"{text}\n\n[Command aborted]", details)
    if code:
        return ToolResult.error(f"{text}\n\n[Exit code {code}]", details)
    return ToolResult.text(text, details)


def _bash(args: dict, ctx: ToolContext) -> ToolResult:
    timeout = min(float(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    out, code, status = run_command(args["command"], ctx.cwd, timeout, ctx.signal)
    return format_output(out, code, status, ctx.cwd, timeout)


BASH_PARAMS = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "timeout": {
            "type": "number",
            "description": f"Seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})",
        },
    },
    "required": ["command"],
}

BASH = AgentTool(
    name="bash",
    label="Bash",
    description=(
        "Run a shell command in the working directory (non-interactive; stdin is closed). "
        "Bash/Zsh stop on unhandled command or pipeline failures; handle expected nonzero statuses explicitly. "
        f"Output is limited to the last {MAX_LINES} lines / {MAX_BYTES // 1024}KB; longer output is saved to a file."
    ),
    parameters=BASH_PARAMS,
    execute=_bash,
    snippet="run shell commands (tests, builds, git, package managers)",
)
