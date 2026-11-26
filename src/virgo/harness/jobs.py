"""Background job supervision owned by Core.

A job is long work that must not block the conversation: a shell command or an
agent child. Starting one returns its id at once; when it finishes, its owner
gets a notification that is injected before the next model call if the owner is
busy, or starts a new turn if it is idle.

Core owns the lifecycle, owner accounting, notifications, cancellation,
cleanup and output budget. Capability plugins wrap this manager with their
model-facing tools and commands; they never own the supervision itself.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.types import ToolResult
from ..diagnostics import capture_error, error_summary, safe_text
from ..execution import ExecutionCancelled, ExecutionFailed, ExecutionUnavailable, cleanup_process
from ..tools.bash import kill_tree, spawn
from ..tools.output import open_output, read_output
from ..tools.shell import sandbox_failure_details

if TYPE_CHECKING:
    from .session import AgentSession


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
    boundary: dict | None = None
    execution_context: object = field(default=None, repr=False)
    related_errors: list[dict] = field(default_factory=list)

    def elapsed(self) -> str:
        return f"{(self.ended or time.time()) - self.started:.0f}s"


class JobManager:
    """Core-owned supervision for background shell commands and agent children."""

    def __init__(self, session: AgentSession, *, background_enabled: bool):
        self.session = session
        # The background capability stays a plugin switch: disabling the
        # background plugin removes the model-facing capability exactly as
        # before, while Core still owns and supervises every started job.
        self.background_enabled = background_enabled
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        # Core supervision outlives capability plugins: it is registered first,
        # so its cleanup runs after those plugins released their own resources.
        session.register_cleanup("core.jobs", self.shutdown)
        session.bus.on("runtime_abort", self._abort_runtime, owner="core.jobs")
        session.bus.on("pending_work", lambda _session: bool(self.unreported()), owner="core.jobs")

    def set_background_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.background_enabled == enabled:
            return
        self.background_enabled = enabled
        self.session.bus.emit("background_capability_changed", enabled)

    def current_store(self):
        with self.session.state_lock:
            return self.session.store

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _new(self, kind: str, label: str, *, store=None, agent="main", tool_call_id=None, cwd=None) -> Job:
        # Capture the owner before a worker can start or the user can switch
        # conversations. Background failures must not move to the selected chat.
        if store is None:
            store = self.current_store()
        # Preserve the submitting branch as well as its conversation. Trusted
        # callers may pass a Store; model dispatch already passes BranchSource.
        from .origin_context import BranchSource

        runtime = self.session.runtime_for_store(store)
        if not isinstance(store, BranchSource):
            if runtime is not None:
                store = runtime.origin.root
        with self._lock:
            job = Job(
                f"job{next(self._ids)}",
                kind,
                label,
                accounting_store=store,
                agent=agent,
                tool_call_id=tool_call_id,
                cwd=cwd or store.cwd,
                execution_context=runtime.execution_context if runtime is not None else None,
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
            "output_id": str(job.log) if job.log else None,
            "duration": round((job.ended or time.time()) - job.started, 6),
            **({"boundary": job.boundary} if job.boundary is not None else {}),
            **(context or {}),
        }
        details = self.session.report_error(
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
                self.session.report_error(
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
            try:
                if job.proc.poll() is None:
                    kill_tree(job.proc)
                    job.proc.wait(timeout=5)
            finally:
                cleanup_process(job.proc)
        except Exception as error:
            self._report(job, error, phase="background.cleanup",
                         kind="fatal" if isinstance(error, ExecutionFailed) else None,
                         code="EXECUTION_FAILED" if isinstance(error, ExecutionFailed) else None)
            return False
        return True

    def _start_failed(self, job: Job, error: BaseException, fh) -> None:
        cancelled = isinstance(error, (KeyboardInterrupt, ExecutionCancelled))
        job.status = "killed" if cancelled else "failed"
        job.ended = time.time()
        if isinstance(error, Exception):
            self._report(job, error, phase="background.start", kind="aborted" if cancelled else "fatal",
                         code="EXECUTION_UNAVAILABLE" if isinstance(error, ExecutionUnavailable) else (
                             "EXECUTION_FAILED" if isinstance(error, ExecutionFailed) else None))
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
        execution_context=None,
        cancellation=None,
    ) -> Job:
        shell = shell or self.session.services["shell"].current
        if shell is None:
            raise ValueError(self.session.t("features.jobs.no_shell"))
        with self.session.state_lock:
            owner = store if store is not None else self.current_store()
            runtime = self.session.runtime_for_store(owner)
            if execution_context is None:
                if runtime is None:
                    raise ValueError("Background execution requires its originating runtime")
                execution_context = runtime.execution_context
            store = owner
        job = self._new(
            shell.tool_name, command, store=store, agent=agent, tool_call_id=tool_call_id, cwd=cwd
        )
        job.execution_context = execution_context
        fh = None
        try:
            job.boundary = execution_context.describe() if execution_context is not None else None
            job.log, fh = open_output(cwd, job.id, **({"execution_context": execution_context} if execution_context else {}))
            proc = spawn(command, cwd, stdout=fh, interpreter=shell,
                         **({"execution_context": execution_context, "cancellation": cancellation} if execution_context else {}))
        except BaseException as error:
            # A job that never started must not count as pending work forever.
            self._start_failed(job, error, fh)
            raise
        job.proc = proc
        job.boundary = getattr(proc, "_virgo_boundary", None) or (
            {**execution_context.describe(), "payload_started": True, "side_effects": "possible", "cleanup": "pending"}
            if execution_context is not None else None)

        def watch():
            status = "failed"
            timed_out = False
            watch_failed = False
            try:
                deadline = time.monotonic() + timeout if timeout else None
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
            except Exception as error:
                watch_failed = True
                status = "killed" if job.signal.is_set() and not isinstance(error, ExecutionFailed) else "failed"
                details = self._report(job, error, phase="background.watch",
                                       kind="fatal" if isinstance(error, ExecutionFailed) else None,
                                       code="EXECUTION_FAILED" if isinstance(error, ExecutionFailed) else None)
                job.result = error_summary(details, t=self.session.t)
            finally:
                # A cancellation request is not proof of process termination.
                # Failed native teardown must remain a failed job.
                if not self._stop_process(job):
                    status = "failed"
                close_error = None
                try:
                    fh.close()
                except Exception as error:
                    close_error = error
                    if status != "killed":
                        status = "failed"
                if job.boundary is not None and job.boundary.get("provider") == "host":
                    job.boundary["cleanup"] = "process_group_best_effort" if os.name != "nt" else "job_object"
                if not watch_failed and status == "failed" and not job.signal.is_set():
                    if not timed_out and job.exit_code and isinstance(job.boundary, dict) and job.log is not None:
                        try:
                            sample = read_output(job.log, max_bytes=4800, execution_context=job.execution_context).text
                        except Exception:
                            sample = ""
                        hint = sandbox_failure_details(job.label, sample, job.boundary, t=self.session.t)
                        if hint is not None:
                            job.boundary["hint"] = hint.to_dict()
                    if timed_out or job.exit_code:
                        self._report(
                            job,
                            f"Background job {job.id} timed out."
                            if timed_out
                            else f"Background job {job.id} exited with status {job.exit_code}.",
                            phase="background.command",
                            code="BACKGROUND_TIMEOUT" if timed_out else "BACKGROUND_EXIT_FAILED",
                            context={"timeout_seconds": timeout} if timed_out else None,
                        )
                if close_error is not None:
                    self._report(job, close_error, phase="background.close")
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
        owner = store if store is not None else self.current_store()
        release = self.session.reserve_child_work(owner)
        try:
            job = self._new("agent", label, store=owner, agent=agent, tool_call_id=tool_call_id, cwd=cwd)
        except BaseException:
            release()
            raise

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
                    original_error = details.get("error")
                    # An extension may return an error result without structured
                    # diagnostics. Preserve its bounded, redacted explanation instead
                    # of replacing it with a generic "job failed" message.
                    reason = original_error or safe_text(text, secrets=(job.label,))[:1024]
                    self._report(
                        job,
                        reason or f"Background job {job.id} reported a failed result.",
                        phase="background.callable",
                        code="BACKGROUND_RESULT_FAILED" if not original_error else None,
                    )
                for related in details.get("related_errors") or []:
                    if isinstance(related, dict):
                        self._report(job, related, phase="background.callable")
            except Exception as e:  # noqa: BLE001
                details = self._report(job, e, phase="background.callable")
                job.result = error_summary(details, t=self.session.t)
                status = "killed" if job.signal.is_set() else "failed"
            finally:
                # Callable/child execution has ended. Release outside the jobs
                # lock and before _finish publishes job.done to waiting callers.
                release()
            self._finish(job, status)

        try:
            threading.Thread(target=run, daemon=True, name=f"virgo-{job.id}").start()
        except BaseException as error:
            release()
            self._start_failed(job, error, None)
            raise
        return job

    def output(self, job: Job) -> str:
        if job.log is not None:
            try:
                return read_output(job.log, execution_context=job.execution_context).text
            except FileNotFoundError:
                return ""
        return job.result

    def kill(self, job: Job) -> None:
        job.signal.set()
        if job.proc is not None:
            try:
                kill_tree(job.proc)
            except Exception as error:
                self._report(job, error, phase="background.cancel",
                             kind="fatal" if isinstance(error, ExecutionFailed) else None,
                             code="EXECUTION_FAILED" if isinstance(error, ExecutionFailed) else None)
                raise

    def _finish(self, job: Job, status: str) -> None:
        job.status = status
        job.ended = time.time()
        try:
            try:
                if job.log is not None:
                    captured = read_output(job.log, max_bytes=4800, execution_context=job.execution_context)
                    out, total = captured.text.strip(), captured.total_bytes
                    unit = "bytes"
                else:
                    out = job.result.strip()
                    total, unit = len(out), "chars"
            except Exception as error:
                if job.status != "killed":
                    job.status = "failed"
                details = self._report(job, error, phase="background.output")
                out = job.result or error_summary(details, t=self.session.t)
                total, unit = len(out), "chars"
            truncated = job.kind != "agent" and total > 1200
            output = out[-1200:] if job.kind != "agent" else out
            code = (
                self.session.t("features.jobs.exit_code", code=job.exit_code)
                if job.exit_code is not None
                else ""
            )
            text = self.session.t(
                "features.jobs.notification",
                id=job.id,
                kind=self.session.t("features.jobs.kind." + job.kind),
                label=job.label[:80],
                status=self.session.t("features.jobs.state." + job.status),
                code=code,
                elapsed=job.elapsed(),
                heading=self.session.t(
                    "features.jobs.result" if job.kind == "agent" else "features.jobs.last_output"
                ),
                output=output or self.session.t("tools.output.empty"),
            )
            if truncated:
                text += self.session.t(
                    "features.jobs.more",
                    total=total,
                    unit=self.session.t("features.jobs.unit." + unit),
                    id=job.id,
                )
            hint = job.boundary.get("hint") if isinstance(job.boundary, dict) else None
            if isinstance(hint, dict) and isinstance(hint.get("message"), str) and hint["message"]:
                text += "\n" + hint["message"]
            if job.error_details is not None:
                text += "\n" + error_summary(job.error_details, t=self.session.t)
            evidence = ([job.error_details] if job.error_details is not None else []) + job.related_errors
            # Retain evidence before delivery, so a broken UI/notification path
            # cannot prevent the original lead from querying it on its next run.
            self.session.retain_diagnostics(job.accounting_store, evidence)
            self.session.notify(text, mode="steer", source="jobs", store=job.accounting_store)
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

    def shutdown(self) -> None:
        """Stop every owned job and bound the wait for its notification."""
        pending = list(self.unreported())
        failure = None
        for job in pending:
            if job.status == "running":
                try:
                    self.kill(job)
                except Exception as error:
                    failure = failure or error  # Try every owned job before reporting cleanup failure.
        deadline = time.monotonic() + 5
        for job in pending:
            job.done.wait(max(0, deadline - time.monotonic()))
        if failure is not None:
            raise failure

    def _abort_runtime(self, runtime) -> None:
        owner = runtime.origin.root
        with self._lock:
            owned = [job for job in self.jobs.values()
                     if job.kind == "agent" and job.status == "running"
                     and job.accounting_store.id == owner.id
                     and (runtime.role == "main" or job.agent == runtime.name)]
        for job in owned:
            self.kill(job)

