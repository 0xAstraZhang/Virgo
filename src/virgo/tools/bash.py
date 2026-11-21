"""bash: run a shell command with timeout, abort, and bounded output."""

from __future__ import annotations

import os
import signal as _signal
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..i18n import Translator
from .output import MAX_BYTES, MAX_LINES, CommandOutput, bound, display_output, open_output, read_output

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
SHELL = os.environ.get("VIRGO_SHELL") or ("bash" if os.name != "nt" else None)


def spawn(command: str, cwd: Path, stdout=subprocess.PIPE, *, interpreter=None, execution_context=None, cancellation=None) -> subprocess.Popen:
    from .shell import shell_environment

    env = shell_environment()
    if interpreter is not None:
        argv, shell = interpreter.argv(command), False
    elif SHELL:
        # Do not let a later ls/head/cat hide a failed download or pipeline.
        # Only add flags for known shells; VIRGO_SHELL may be another interpreter.
        kind = Path(SHELL).name.lower().removesuffix(".exe")
        options = ["-e", "-o", "pipefail"] if kind in {"bash", "zsh"} else []
        argv: list[str] | str = [SHELL, *options, "-c", command]
        shell = False
    else:
        argv, shell = command, True
    if execution_context is not None:
        if shell or isinstance(argv, str):
            raise ValueError("Managed execution requires an explicit interpreter argv")
        from ..execution import ProcessRequest

        request = ProcessRequest(tuple(argv), cwd, env, stdout=stdout)
        return execution_context.run_process(request, cancellation)
    if os.name == "nt":
        from .windows_process import WindowsProcess

        if shell:
            comspec = os.environ.get("ComSpec") or str(
                Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
            )
            argv = f'"{comspec}" /d /s /c "{command}"'
        return WindowsProcess(argv, cwd=cwd, env=env, stdout=stdout)
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
    terminate_tree = getattr(proc, "kill_tree", None)
    if terminate_tree is not None:
        terminate_tree()
        return
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
            # The Windows process wrapper terminates its Job Object, not just the root PID.
            proc.kill()
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_command(
    command: str, cwd: Path, timeout: float, signal: threading.Event, *, interpreter=None, execution_context=None
) -> tuple[CommandOutput, int | None, str]:
    """Spool stdout/stderr directly to disk, returning only a bounded tail and metadata."""
    if signal.is_set():
        return CommandOutput(""), None, "aborted"
    prefix = interpreter.tool_name if interpreter is not None else "bash"
    record, output_file = open_output(cwd, prefix, **({"execution_context": execution_context} if execution_context else {}))
    with output_file:
        try:
            proc = spawn(command, cwd, stdout=output_file, interpreter=interpreter,
                         **({"execution_context": execution_context, "cancellation": signal} if execution_context else {}))
        except BaseException:
            output_file.close()
            record.unlink(missing_ok=True)
            raise
        deadline = time.monotonic() + timeout
        status = "ok"
        try:
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
        finally:
            from ..execution import cleanup_process

            try:
                if proc.poll() is None:
                    kill_tree(proc)
                    proc.wait(timeout=5)
            finally:
                cleanup_process(proc)
    boundary = getattr(proc, "_virgo_boundary", None)
    if boundary is None and execution_context is not None:
        boundary = {**execution_context.describe(), "payload_started": True, "side_effects": "possible", "cleanup": "process_group_best_effort" if os.name != "nt" else "job_object"}
    captured = read_output(record, execution_context=execution_context)
    # S3-09: Normal un-truncated foreground commands must not leak spool files on disk.
    needs_record = captured.truncated or len(captured.text.rstrip().encode("utf-8", errors="replace")) > MAX_BYTES - 256
    if not needs_record:
        record.unlink(missing_ok=True)
        captured = replace(captured, path=None)
    return replace(captured, boundary=boundary), proc.returncode, status


def format_output(
    output: CommandOutput | str,
    code: int | None,
    status: str,
    cwd: Path,
    timeout: float,
    *,
    prefix="bash",
    t=None,
) -> ToolResult:
    t = t or Translator()
    if isinstance(output, str):
        # Compatibility for extensions that format a pre-existing in-memory result.
        output = bound(output, cwd, prefix, keep="tail")
    text = display_output(output, max_bytes=MAX_BYTES - 256, t=t)
    details = {
        "exit_code": code,
        "status": status,
        "truncated": output.truncated
        or len((output.text.rstrip() or t("tools.output.empty")).encode("utf-8", errors="replace")) > MAX_BYTES - 256,
        "output_bytes": output.total_bytes,
        "output_id": str(output.path) if output.path else None,
    }
    if status == "timeout":
        return ToolResult.error(t("tools.output.timeout", text=text, timeout=timeout), details)
    if status == "aborted":
        details["cancelled"] = True
        return ToolResult.error(t("tools.output.aborted", text=text), details)
    if code:
        return ToolResult.error(t("tools.output.exit", text=text, code=code), details)
    return ToolResult.text(text, details)


def _bash(args: dict, ctx: ToolContext) -> ToolResult:
    timeout = min(float(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    out, code, status = run_command(args["command"], ctx.cwd, timeout, ctx.signal)
    return format_output(out, code, status, ctx.cwd, timeout, t=ctx.t)


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
        f"Output is limited to the last {MAX_LINES} lines / {MAX_BYTES // 1024}KB; longer output is saved with an "
        "output_id for read_output."
    ),
    parameters=BASH_PARAMS,
    execute=_bash,
    execution_scope="process",
    i18n_key="tools.bash",
)
