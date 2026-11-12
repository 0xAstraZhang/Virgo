"""Background jobs: long commands and sub-agents run without blocking the conversation.

``bash{background:true}`` and ``agent{background:true}`` start a job and return its id at once.
When a job finishes, the lead agent gets a notification: injected before its next model call if it
is busy, or starting a new turn if it is idle.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..harness.extensions import ExtensionAPI
from ..tools.bash import kill_tree, spawn
from ..tools.command_output import display_output, read_output
from ..tools.truncate import MAX_BYTES, budget, outputs_dir


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "running"  # running | completed | failed | killed
    started: float = field(default_factory=time.time)
    ended: float | None = None
    exit_code: int | None = None
    log: Path | None = None
    result: str = ""
    signal: threading.Event = field(default_factory=threading.Event)
    proc: object = None
    reported: bool = False  # its completion notification has been delivered

    def elapsed(self) -> str:
        return f"{(self.ended or time.time()) - self.started:.0f}s"


class JobManager:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _new(self, kind: str, label: str) -> Job:
        with self._lock:
            job = Job(f"job{next(self._ids)}", kind, label)
            self.jobs[job.id] = job
            return job

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    def unreported(self) -> list[Job]:
        """Running jobs, and finished ones whose notification is still being delivered."""
        return [j for j in self.jobs.values() if not j.reported]

    def start_command(self, command: str, cwd: Path, timeout: float | None = None, *, shell=None) -> Job:
        shell = shell or self.api.services["shell"].current
        if shell is None:
            raise ValueError("No shell is available; use /shell to select one.")
        job = self._new(shell.tool_name, command)
        fh = None
        try:
            job.log = outputs_dir(cwd) / f"{job.id}-{int(job.started)}.log"
            fh = open(job.log, "wb")  # noqa: SIM115 - closed by the watcher thread
            proc = spawn(command, cwd, stdout=fh, interpreter=shell)
        except BaseException:
            # A job that never started must not count as pending work forever.
            if fh is not None:
                fh.close()
                job.log.unlink(missing_ok=True)
            with self._lock:
                self.jobs.pop(job.id, None)
            raise
        job.proc = proc

        def watch():
            deadline = time.monotonic() + timeout if timeout else None
            timed_out = False
            while proc.poll() is None:
                if job.signal.is_set() or (deadline and time.monotonic() > deadline):
                    timed_out = not job.signal.is_set()
                    kill_tree(proc)
                    break
                time.sleep(0.1)
            proc.wait()
            fh.close()
            job.exit_code = proc.returncode
            self._finish(
                job, "killed" if job.signal.is_set() else ("completed" if proc.returncode == 0 and not timed_out else "failed")
            )

        threading.Thread(target=watch, daemon=True, name=f"virgo-{job.id}").start()
        return job

    def start_callable(self, label: str, fn: Callable[[threading.Event], ToolResult | str]) -> Job:
        job = self._new("agent", label)

        def run():
            try:
                res = fn(job.signal)
                text = res if isinstance(res, str) else res.as_text()
                failed = not isinstance(res, str) and res.is_error
                job.result = text
                status = "killed" if job.signal.is_set() else ("failed" if failed else "completed")
            except Exception as e:  # noqa: BLE001
                job.result = f"{type(e).__name__}: {e}"
                status = "failed"
            self._finish(job, status)

        threading.Thread(target=run, daemon=True, name=f"virgo-{job.id}").start()
        return job

    def output(self, job: Job) -> str:
        if job.log is not None:
            try:
                return read_output(job.log).text
            except FileNotFoundError:
                return ""
        return job.result

    def kill(self, job: Job) -> None:
        job.signal.set()
        if job.proc is not None:
            kill_tree(job.proc)

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.ended = time.time()
        try:
            if job.log is not None:
                captured = read_output(job.log, max_bytes=4800)
                out, total = captured.text.strip(), captured.total_bytes
                unit = "bytes"
            else:
                out = job.result.strip()
                total, unit = len(out), "chars"
            tail = out[-1200:] if len(out) > 1200 else out
            code = f", exit code {job.exit_code}" if job.exit_code is not None else ""
            text = (
                f"Background job {job.id} ({job.kind}: {job.label[:80]}) {job.status}{code} after {job.elapsed()}.\n"
                f"{'Result' if job.kind == 'agent' else 'Last output'}:\n{tail or '(no output)'}"
            )
            if total > 1200:
                text += f"\n[{total} {unit} total — use job_output id={job.id} for the log path and bounded output]"
            self.api.notify(text, mode="steer", source="jobs")
        finally:
            # Idle detection waits for this flag, not the status: the
            # notification must be queued before print mode can finish.
            job.reported = True


def setup(api: ExtensionAPI) -> None:
    jobs = JobManager(api)
    api.services["jobs"] = jobs

    api.services["shell"].install_tools()

    def find(args: dict) -> Job | ToolResult:
        job = jobs.jobs.get(args["id"])
        return (
            job if job else ToolResult.error(f"No job {args['id']}. Known: {', '.join(jobs.jobs) or 'none'}")
        )

    def output(args: dict, ctx: ToolContext) -> ToolResult:
        job = find(args)
        if isinstance(job, ToolResult):
            return job
        wait = min(float(args.get("wait") or 0), 600)
        end = time.time() + wait
        while job.status == "running" and time.time() < end and not ctx.signal.is_set():
            time.sleep(0.2)
        label = job.label if len(job.label) <= 300 else job.label[:300] + "…"
        head = f"{job.id} [{job.status}] {job.kind}: {label} ({job.elapsed()})"
        if job.log is not None:
            captured = read_output(job.log)
            displayed = display_output(captured, max_bytes=MAX_BYTES - len(head.encode("utf-8")) - 1)
            return ToolResult.text(head + "\n" + displayed, {
                "output_path": str(job.log), "output_bytes": captured.total_bytes,
                "truncated": displayed.startswith("[Output truncated:"),
            })
        return ToolResult.text(head + "\n" + budget(job.result or "(no output yet)", ctx.cwd, job.id, keep="tail"))

    def kill(args: dict, ctx: ToolContext) -> ToolResult:
        job = find(args)
        if isinstance(job, ToolResult):
            return job
        jobs.kill(job)
        return ToolResult.text(f"Stopping {job.id}.")

    api.register_tool(
        AgentTool(
            "job_output",
            "Show a background job's status and output. wait=N blocks up to N seconds for it to finish.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}, "wait": {"type": "number"}},
                "required": ["id"],
            },
            output,
            snippet="read a background job's output",
            read_only=True,
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "job_kill",
            "Stop a background job.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            kill,
            snippet="stop a background job",
            read_only=True,
        ),
        roles=("main",),
    )

    def cmd(session, args):
        if not jobs.jobs:
            api.ui.print("No background jobs.")
            return
        api.ui.print(
            "\n".join(
                f"  {j.id} [{j.status}] {j.kind}: {j.label[:70]} ({j.elapsed()})" for j in jobs.jobs.values()
            )
        )

    api.register_command("jobs", cmd, "list background jobs", concurrent=True)
    api.on("pending_work", lambda s: bool(jobs.unreported()))

    def shutdown(session):
        for j in jobs.active():
            jobs.kill(j)

    api.on("session_shutdown", shutdown)
