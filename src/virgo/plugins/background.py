"""Background capability: model-facing job tools and listing on top of Core supervision.

``bash{background:true}`` and ``agent{background:true}`` start a job and return its id at once.
Core owns the job lifecycle, owner accounting, notifications, cancellation and
cleanup; this plugin only wraps that manager with tools and a command.
"""

from __future__ import annotations

import time

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..diagnostics import error_summary
from ..harness.extensions import ExtensionAPI
from ..harness.jobs import Job, JobManager
from ..tools.command_output import display_output, read_output
from ..tools.truncate import MAX_BYTES, budget


def setup(api: ExtensionAPI) -> None:
    jobs: JobManager = api.services["jobs"]

    def find(args: dict) -> Job | ToolResult:
        job = jobs.get(args["id"])
        return (
            job
            if job
            else ToolResult.error(
                api.t(
                    "features.jobs.missing",
                    id=args["id"],
                    known=", ".join(jobs.jobs) or api.t("features.jobs.none"),
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
        if job.sandbox is not None:
            details["sandbox"] = job.sandbox
        if job.error_details is not None:
            details.update(error=job.error_details, related_errors=job.related_errors)
            head += "\n" + error_summary(job.error_details, t=api.t)
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
            side_effect="read",
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
            side_effect="read",
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
