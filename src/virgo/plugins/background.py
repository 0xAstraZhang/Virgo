"""Background capability: model-facing job tools and listing on top of Core supervision.

``bash{background:true}`` and ``agent{background:true}`` start a job and return its id at once.
Core owns the job lifecycle, owner accounting, notifications, cancellation and
cleanup; this plugin only wraps that manager with tools and a command.
"""

from __future__ import annotations

import time

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..diagnostics import error_summary
from ..execution import FileAccessDenied
from ..harness.extensions import ExtensionAPI
from ..harness.jobs import Job, JobManager
from ..tools.output import bound, display_output, read_output

JOB_OUTPUT_PREVIEW_BYTES = 8 * 1024


def setup(api: ExtensionAPI) -> None:
    jobs: JobManager = api.services["jobs"]

    def find(args: dict, ctx: ToolContext) -> Job | ToolResult:
        runtime = api.runtime_for_context(ctx)
        if runtime is None:
            return ToolResult.error("Background operations require their originating task context")
        job = jobs.get(args["id"])
        if job is not None and job.accounting_store is not ctx.origin.root:
            return ToolResult.error("This background job belongs to another task")
        return (
            job
            if job
            else ToolResult.error(
                api.t(
                    "features.jobs.missing",
                    id=args["id"],
                    known=", ".join(j.id for j in jobs.jobs.values() if j.accounting_store is ctx.origin.root)
                    or api.t("features.jobs.none"),
                )
            )
        )

    def output(args: dict, ctx: ToolContext) -> ToolResult:
        job = find(args, ctx)
        if isinstance(job, ToolResult):
            return job
        wait = min(float(args.get("wait") or 0), 600)
        end = time.time() + wait
        while job.status == "running" and time.time() < end and not ctx.signal.is_set():
            time.sleep(0.2)
        label = job.label if len(job.label) <= 300 else job.label[:300] + "…"
        head = f"{job.id} [{ctx.t('features.jobs.state.' + job.status)}] {ctx.t('features.jobs.kind.' + job.kind)}: {label} ({job.elapsed()})"
        details = {"job_id": job.id, "status": job.status, "exit_code": job.exit_code}
        if job.boundary is not None:
            details["boundary"] = job.boundary
        if job.error_details is not None:
            details.update(error=job.error_details, related_errors=job.related_errors)
            head += "\n" + error_summary(job.error_details, t=api.t)
        heading = head.encode("utf-8")
        # A long diagnostic can use any space left after the output ID and tail are rendered.
        reserved_bytes = min(len(heading) + 1, JOB_OUTPUT_PREVIEW_BYTES // 2)
        body_bytes = JOB_OUTPUT_PREVIEW_BYTES - reserved_bytes
        if job.log is not None:
            try:
                captured = read_output(job.log, max_bytes=body_bytes, execution_context=job.execution_context)
            except FileAccessDenied as error:
                return ToolResult.error(str(error), details={**details, "status": "resource_denied", "boundary": {
                    **job.execution_context.describe(), "kind": "resource_denied", "dimension": error.dimension,
                    "target": error.target, "payload_started": False, "side_effects": "none",
                }})
        else:
            captured = bound(
                job.result, ctx.cwd, job.id, keep="tail", max_bytes=body_bytes,
                execution_context=api.services["execution"].for_context(ctx),
            )
        if job.log is None and not captured.truncated:
            displayed = captured.text or ctx.t("features.jobs.no_output")
        else:
            displayed = display_output(
                captured, max_bytes=JOB_OUTPUT_PREVIEW_BYTES, reserved_bytes=reserved_bytes, t=ctx.t,
            )
        heading_limit = JOB_OUTPUT_PREVIEW_BYTES - len(displayed.encode("utf-8")) - 1
        heading_cut = len(heading) > heading_limit
        if heading_cut:
            head = heading[:heading_limit - len("…".encode())].decode("utf-8", errors="ignore") + "…"
        return ToolResult.text(
            head + "\n" + displayed,
            {
                **details,
                "output_id": str(captured.path) if captured.path else None,
                "output_bytes": captured.total_bytes,
                "truncated": heading_cut or captured.truncated,
            },
        )

    def kill(args: dict, ctx: ToolContext) -> ToolResult:
        job = find(args, ctx)
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
            side_effect="read",
            execution_scope="broker",
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
            side_effect="write",
            execution_scope="broker",
            i18n_key="tools.job_kill",
        ),
        roles=("main",),
    )

    def cmd(session, args):
        if not jobs.jobs:
            api.ui.print(api.t("features.jobs.empty"))
            return
        api.ui.print(
            "\n".join(
                f"  {j.id} [{api.t('features.jobs.state.' + j.status)}] {api.t('features.jobs.kind.' + j.kind)}: {j.label[:70]} ({j.elapsed()})"
                for j in jobs.jobs.values()
            )
        )

    api.register_command(
        "jobs", cmd, "list background jobs", concurrent=True, description_key="features.commands.jobs"
    )
