"""Background jobs: long commands and sub-agents run without blocking the conversation.

``bash{background:true}`` and ``agent{background:true}`` start a job and return its id at once.
When a job finishes, the lead agent gets a notification: injected before its next model call if it
is busy, or starting a new turn if it is idle.
"""

from __future__ import annotations

import itertools
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..diagnostics import capture_error, error_summary, safe_text
from ..harness.extensions import ExtensionAPI
from ..tools.bash import kill_tree, spawn
from ..tools.command_output import display_output, open_output, read_output
from ..tools.truncate import MAX_BYTES, budget


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
    done: threading.Event = field(default_factory=threading.Event)
    accounting_store: object = field(default=None, repr=False)
    agent: str = "main"
    tool_call_id: str | None = None
    cwd: Path | None = None
    error_details: dict | None = None
    related_errors: list[dict] = field(default_factory=list)

    def elapsed(self) -> str:
        return f"{(self.ended or time.time()) - self.started:.0f}s"


class JobManager:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _new(self, kind: str, label: str, *, store=None, agent="main", tool_call_id=None, cwd=None) -> Job:
        # Capture the owner before a worker can start or the user can switch
        # conversations. Background failures must not move to the selected chat.
        if store is None:
            with self.api.session._lock:
                store = self.api.session.store
        with self._lock:
            job = Job(
                f"job{next(self._ids)}",
                kind,
                label,
                accounting_store=store,
                agent=agent,
                tool_call_id=tool_call_id,
                cwd=cwd or store.cwd,
            )
            self.jobs[job.id] = job
            return job

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    def unreported(self) -> list[Job]:
        """Running jobs, and finished ones whose notification is still being delivered."""
        return [j for j in self.jobs.values() if not j.reported]

    def _report(self, job: Job, error, *, phase: str, kind=None, code=None, context=None) -> dict:
        kind = kind or ("aborted" if job.signal.is_set() else "fatal")
        try:
            details = error if isinstance(error, dict) else getattr(error, "_virgo_error_details", None)
        except Exception:
            details = None
        if not isinstance(details, dict) or not isinstance(details.get("error_id"), str):
            details = capture_error(
                error.get("message", "Background operation failed.") if isinstance(error, dict) else error,
                phase=phase,
                kind=kind,
                code=code,
                secrets=(job.label,),
                message=f"Background process did not exit within {error.timeout} seconds."
                if isinstance(error, subprocess.TimeoutExpired)
                else None,
            )
        metadata = {
            "job_id": job.id,
            "job_kind": job.kind,
            "agent": job.agent,
            "tool_call_id": job.tool_call_id,
            "status": ("killed" if kind == "aborted" else "failed")
            if job.status == "running"
            else job.status,
            "exit_code": job.exit_code,
            "cwd": str(job.cwd),
            "output_path": str(job.log) if job.log else None,
            "duration": round((job.ended or time.time()) - job.started, 6),
            **(context or {}),
        }
        details = self.api.session.report_error(
            details,
            phase=phase,
            store=job.accounting_store,
            notify=kind != "aborted",
            context=metadata,
        )
        if isinstance(error, BaseException):
            try:
                error._virgo_error_details = details
            except Exception:
                pass  # Custom exception types may forbid attaching optional metadata.
        if job.error_details is None:
            job.error_details = details
        elif details["error_id"] != job.error_details["error_id"] and all(
            item["error_id"] != details["error_id"] for item in job.related_errors
        ):
            job.related_errors.append(details)
        if details.get("context", {}).get("job_id") != job.id and not any(
            item.get("code") == "BACKGROUND_ERROR_LINK"
            and item.get("context", {}).get("related_error_id") == details["error_id"]
            for item in job.related_errors
        ):
            # A child runtime may already have persisted this error ID. Keep
            # that cause intact and record the job association independently.
            link = capture_error(
                f"Background job {job.id} failed; see error {details['error_id']}.",
                phase="background.result",
                code="BACKGROUND_ERROR_LINK",
                kind=details.get("kind", kind),
                context={**metadata, "related_error_id": details["error_id"]},
            )
            job.related_errors.append(
                self.api.session.report_error(
                    link,
                    phase="background.result",
                    store=job.accounting_store,
                    notify=False,
                )
            )
        return details

    def _stop_process(self, job: Job) -> bool:
        if job.proc is None:
            return True
        try:
            if job.proc.poll() is None:
                kill_tree(job.proc)
                job.proc.wait(timeout=5)
        except Exception as error:
            self._report(job, error, phase="background.cleanup")
            return False
        return True

    def _start_failed(self, job: Job, error: BaseException, fh) -> None:
        job.status = "killed" if isinstance(error, KeyboardInterrupt) else "failed"
        job.ended = time.time()
        if isinstance(error, Exception):
            self._report(job, error, phase="background.start")
        self._stop_process(job)
        if fh is not None:
            try:
                fh.close()
            except Exception as cleanup_error:
                self._report(job, cleanup_error, phase="background.cleanup")
            try:
                job.log.unlink(missing_ok=True)
            except Exception as cleanup_error:
                self._report(job, cleanup_error, phase="background.cleanup")
        with self._lock:
            self.jobs.pop(job.id, None)
        job.reported = True
        job.done.set()

    def start_command(
        self,
        command: str,
        cwd: Path,
        timeout: float | None = None,
        *,
        shell=None,
        store=None,
        agent="main",
        tool_call_id=None,
    ) -> Job:
        shell = shell or self.api.services["shell"].current
        if shell is None:
            raise ValueError(self.api.session.t('features.jobs.no_shell'))
        job = self._new(
            shell.tool_name, command, store=store, agent=agent, tool_call_id=tool_call_id, cwd=cwd
        )
        fh = None
        try:
            job.log, fh = open_output(cwd, job.id)
            proc = spawn(command, cwd, stdout=fh, interpreter=shell)
        except BaseException as error:
            # A job that never started must not count as pending work forever.
            self._start_failed(job, error, fh)
            raise
        job.proc = proc

        def watch():
            status = "failed"
            try:
                deadline = time.monotonic() + timeout if timeout else None
                timed_out = False
                while proc.poll() is None:
                    if job.signal.is_set() or (deadline and time.monotonic() > deadline):
                        timed_out = not job.signal.is_set()
                        kill_tree(proc)
                        break
                    time.sleep(0.1)
                proc.wait(timeout=5)
                job.exit_code = proc.returncode
                status = (
                    "killed"
                    if job.signal.is_set()
                    else ("completed" if proc.returncode == 0 and not timed_out else "failed")
                )
                if status == "failed":
                    self._report(
                        job,
                        f"Background job {job.id} timed out."
                        if timed_out
                        else f"Background job {job.id} exited with status {job.exit_code}.",
                        phase="background.command",
                        code="BACKGROUND_TIMEOUT" if timed_out else "BACKGROUND_EXIT_FAILED",
                        context={"timeout_seconds": timeout} if timed_out else None,
                    )
            except Exception as error:
                status = "killed" if job.signal.is_set() else "failed"
                details = self._report(job, error, phase="background.watch")
                job.result = error_summary(details, t=self.api.session.t)
            finally:
                if not self._stop_process(job) and status != "killed":
                    status = "failed"
                try:
                    fh.close()
                except Exception as error:
                    if status != "killed":
                        status = "failed"
                    self._report(job, error, phase="background.close")
                self._finish(job, status)

        try:
            threading.Thread(target=watch, daemon=True, name=f"virgo-{job.id}").start()
        except BaseException as error:
            self._start_failed(job, error, fh)
            raise
        return job

    def start_callable(
        self,
        label: str,
        fn: Callable[[threading.Event], ToolResult | str],
        *,
        store=None,
        agent="main",
        tool_call_id=None,
        cwd=None,
    ) -> Job:
        job = self._new("agent", label, store=store, agent=agent, tool_call_id=tool_call_id, cwd=cwd)

        def run():
            try:
                res = fn(job.signal)
                text = res if isinstance(res, str) else res.as_text()
                failed = not isinstance(res, str) and res.is_error
                job.result = text
                status = "killed" if job.signal.is_set() else ("failed" if failed else "completed")
                details = res.details if isinstance(res, ToolResult) and isinstance(res.details, dict) else {}
                if details.get("status") == "aborted":
                    status = "killed"
                if status == "failed":
                    self._report(
                        job,
                        details.get("error") or f"Background job {job.id} reported a failed result.",
                        phase="background.callable",
                    )
                for related in details.get("related_errors") or []:
                    if isinstance(related, dict):
                        self._report(job, related, phase="background.callable")
            except Exception as e:  # noqa: BLE001
                details = self._report(job, e, phase="background.callable")
                job.result = error_summary(details, t=self.api.session.t)
                status = "killed" if job.signal.is_set() else "failed"
            self._finish(job, status)

        try:
            threading.Thread(target=run, daemon=True, name=f"virgo-{job.id}").start()
        except BaseException as error:
            self._start_failed(job, error, None)
            raise
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
            try:
                kill_tree(job.proc)
            except Exception as error:
                self._report(job, error, phase="background.cancel")
                raise

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.ended = time.time()
        try:
            try:
                if job.log is not None:
                    captured = read_output(job.log, max_bytes=4800)
                    out, total = captured.text.strip(), captured.total_bytes
                    unit = "bytes"
                else:
                    out = job.result.strip()
                    total, unit = len(out), "chars"
            except Exception as error:
                if job.status != "killed":
                    job.status = "failed"
                details = self._report(job, error, phase="background.output")
                out = job.result or error_summary(details, t=self.api.session.t)
                total, unit = len(out), "chars"
            tail = out[-1200:] if len(out) > 1200 else out
            code = (
                self.api.session.t("features.jobs.exit_code", code=job.exit_code)
                if job.exit_code is not None
                else ""
            )
            text = self.api.session.t(
                "features.jobs.notification",
                id=job.id,
                kind=self.api.session.t("features.jobs.kind." + job.kind),
                label=job.label[:80],
                status=self.api.session.t("features.jobs.state." + job.status),
                code=code,
                elapsed=job.elapsed(),
                heading=self.api.session.t(
                    "features.jobs.result" if job.kind == "agent" else "features.jobs.last_output"
                ),
                output=tail or self.api.session.t("tools.output.empty"),
            )
            if total > 1200:
                text += self.api.session.t(
                    "features.jobs.more",
                    total=total,
                    unit=self.api.session.t("features.jobs.unit." + unit),
                    id=job.id,
                )
            if job.error_details is not None:
                text += "\n" + error_summary(job.error_details, t=self.api.session.t)
            evidence = ([job.error_details] if job.error_details is not None else []) + job.related_errors
            # Retain evidence before delivery, so a broken UI/notification path
            # cannot prevent the original lead from querying it on its next run.
            self.api.session.retain_diagnostics(job.accounting_store, evidence)
            self.api.notify(text, mode="steer", source="jobs", store=job.accounting_store)
        except Exception as e:  # noqa: BLE001
            # Leaving the job unreported would hold print mode until its idle timeout;
            # surface the lost notification instead so the user still learns the outcome.
            details = capture_error(
                e,
                phase="background.notify",
                message=(f"could not deliver the notification for {job.id} ({job.status}): {safe_text(e)}"),
                context={"related_error_id": job.error_details["error_id"] if job.error_details else None},
                secrets=(job.label,),
            )
            self._report(job, details, phase="background.notify")
        finally:
            # Idle detection waits for this flag, not the status: the notification
            # must be queued (or its failure reported) before print mode can finish.
            job.reported = True
            job.done.set()


def setup(api: ExtensionAPI) -> None:
    jobs = JobManager(api)
    api.services["jobs"] = jobs

    api.services["shell"].install_tools()

    def find(args: dict) -> Job | ToolResult:
        job = jobs.jobs.get(args["id"])
        return (
            job
            if job
            else ToolResult.error(
                api.session.t(
                    "features.jobs.missing",
                    id=args["id"],
                    known=", ".join(jobs.jobs) or api.session.t("features.jobs.none"),
                )
            )
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
        head = f"{job.id} [{ctx.t('features.jobs.state.' + job.status)}] {ctx.t('features.jobs.kind.' + job.kind)}: {label} ({job.elapsed()})"
        details = {"job_id": job.id, "status": job.status, "exit_code": job.exit_code}
        if job.error_details is not None:
            details.update(error=job.error_details, related_errors=job.related_errors)
            head += "\n" + error_summary(job.error_details, t=api.session.t)
        if job.log is not None:
            captured = read_output(job.log)
            displayed = display_output(captured, max_bytes=MAX_BYTES - len(head.encode("utf-8")) - 1, t=ctx.t)
            return ToolResult.text(
                head + "\n" + displayed,
                {
                    **details,
                    "output_path": str(job.log),
                    "output_bytes": captured.total_bytes,
                    "truncated": captured.truncated
                    or len((captured.text.rstrip() or ctx.t("tools.output.empty")).encode("utf-8"))
                    > MAX_BYTES - len(head.encode("utf-8")) - 1,
                },
            )
        return ToolResult.text(
            head
            + "\n"
            + budget(job.result or ctx.t("features.jobs.no_output"), ctx.cwd, job.id, keep="tail", t=ctx.t),
            details,
        )

    def kill(args: dict, ctx: ToolContext) -> ToolResult:
        job = find(args)
        if isinstance(job, ToolResult):
            return job
        jobs.kill(job)
        return ToolResult.text(ctx.t("features.jobs.stopping", id=job.id))

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
            read_only=True,
            i18n_key="tools.job_output",
        ),
        roles=("main",),
    )
    api.register_tool(
        AgentTool(
            "job_kill",
            "Stop a background job.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            kill,
            read_only=True,
            i18n_key="tools.job_kill",
        ),
        roles=("main",),
    )

    def cmd(session, args):
        if not jobs.jobs:
            api.ui.print(api.session.t("features.jobs.empty"))
            return
        api.ui.print(
            "\n".join(
                f"  {j.id} [{api.session.t('features.jobs.state.' + j.status)}] {api.session.t('features.jobs.kind.' + j.kind)}: {j.label[:70]} ({j.elapsed()})"
                for j in jobs.jobs.values()
            )
        )

    api.register_command(
        "jobs", cmd, "list background jobs", concurrent=True, description_key="features.commands.jobs"
    )
    api.on("pending_work", lambda s: bool(jobs.unreported()))

    def shutdown(session):
        for j in jobs.active():
            jobs.kill(j)

    api.on("session_shutdown", shutdown)
