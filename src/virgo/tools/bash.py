"""Managed shell command execution with timeout, abort, and bounded output."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

from ..agent.types import ToolResult
from ..execution_types import ProcessRequest, cleanup_process
from ..i18n import Translator
from .output import MAX_BYTES, CommandOutput, bound, display_output, open_output, read_output
from .shell_env import shell_environment

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600

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


def spawn(command: str, cwd: Path, stdout=subprocess.PIPE, *, interpreter, execution_context, cancellation=None):
    if interpreter is None or execution_context is None:
        raise ValueError("Managed execution requires both an explicit interpreter and execution_context")
    env = shell_environment()
    argv = interpreter.argv(command)
    request = ProcessRequest(tuple(argv), cwd, env, stdout=stdout)
    return execution_context.run_process(request, cancellation)


def kill_tree(proc) -> None:
    terminate_tree = getattr(proc, "kill_tree", None)
    if terminate_tree is not None:
        terminate_tree()
        return
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def run_command(
    command: str, cwd: Path, timeout: float, signal: threading.Event, *, interpreter, execution_context
) -> tuple[CommandOutput, int | None, str]:
    """Spool stdout/stderr directly to disk, returning only a bounded tail and metadata."""
    if signal.is_set():
        return CommandOutput(""), None, "aborted"
    prefix = interpreter.tool_name
    record, output_file = open_output(cwd, prefix, execution_context=execution_context)
    with output_file:
        try:
            proc = spawn(
                command, cwd, stdout=output_file, interpreter=interpreter,
                execution_context=execution_context, cancellation=signal,
            )
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
            try:
                if proc.poll() is None:
                    kill_tree(proc)
                    proc.wait(timeout=5)
            finally:
                cleanup_process(proc)
    boundary = getattr(proc, "_virgo_boundary", None)
    if boundary is None:
        boundary = {
            **execution_context.describe(),
            "payload_started": True,
            "side_effects": "possible",
            "cleanup": "process_group_best_effort" if os.name != "nt" else "job_object",
        }
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
    text = display_output(output, reserved_bytes=256, t=t)
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
