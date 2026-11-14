"""Sub-agents: delegate focused work with fresh contexts and inherited limits.

The read_only capability restricts child tools and shell permissions, not the parent.
"""

from __future__ import annotations

import itertools

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import AssistantMessage, ToolResultMessage, UserMessage
from ..diagnostics import safe_text
from ..harness.extensions import ExtensionAPI

_counter = itertools.count(1)
READ_ONLY_TOOLS = frozenset({"read", "glob", "grep", "bash"})
READ_ONLY_PROMPT = "You are in read-only mode; file and system changes are not permitted."


class Subagents:
    def __init__(self, api: ExtensionAPI):
        self.api = api

    def run(
        self, prompt: str, description: str, signal, cwd=None, review_parent=None,
        *, read_only: bool = False,
    ) -> ToolResult:
        """Run a child with the caller's authority and optional read-only limits."""
        api = self.api
        review_parent = review_parent or api.current_runtime().review_source
        parent = review_parent.runtime
        owner = review_parent.root
        tools = (
            [tool for tool in api.tools_for("subagent") if tool.name in READ_ONLY_TOOLS]
            if read_only else None
        )
        name = f"subagent#{next(_counter)}"
        rt = None
        failure = cleanup_error = None
        try:
            api.ui.print(f"[{name}] {description}", "agent")
            rt = api.create_runtime(
                name,
                "subagent",
                parent=parent,
                permission="plan" if read_only else None,
                cwd=cwd,
                tools=tools,
                system_extra=READ_ONLY_PROMPT if read_only else None,
                max_turns=int(api.settings.get("subagents.maxTurns", 50)),
                signal=signal,
                compaction=True,
                review_parent=review_parent,
            )
            rt.agent.prompt([UserMessage(prompt)])
        except Exception as error:
            failure = api.report_error(
                error, phase="subagent.run", store=owner, context={"agent": name, "role": "subagent"}
            )
        finally:
            if rt is not None:
                try:
                    api.release_runtime(rt)
                except Exception as error:
                    cleanup_error = api.report_error(
                        error,
                        phase="subagent.cleanup",
                        store=owner,
                        context={"agent": name, "role": "subagent"},
                    )
        last = (
            next((m for m in reversed(rt.messages) if isinstance(m, AssistantMessage)), None) if rt else None
        )
        details = {"agent": name}
        if failure is not None:
            details["error"] = failure
        elif last is not None and last.error_details is not None:
            details["error"] = last.error_details
        elif cleanup_error is not None:
            details["error"] = cleanup_error
        if cleanup_error is not None and cleanup_error != details.get("error"):
            details["related_errors"] = [cleanup_error]

        def failed(text):
            results = (
                [message for message in rt.messages if isinstance(message, ToolResultMessage)] if rt else []
            )
            if results:
                recent = [
                    {
                        "tool": message.tool_name,
                        "call_id": message.tool_call_id,
                        "outcome": "returned_error" if message.is_error else "returned_success",
                    }
                    for message in results[-8:]
                ]
                details["child_progress"] = {
                    "completed_calls": sum(not message.is_error for message in results),
                    "recent_tools": recent,
                    "side_effects": "unknown",
                }
                text += api.t("features.agent.progress", count=len(results)) + "\n".join(
                    f"- {item['tool']} ({item['call_id']}): {item['outcome']}" for item in recent
                )
            return ToolResult.error(text, details=details)

        if failure is not None or cleanup_error is not None:
            return failed(api.t("features.agent.failed", error=details["error"]["message"]))
        cancelled = (signal is not None and signal.is_set()) or (rt is not None and rt.agent.signal.is_set())
        if cancelled and (last is None or last.stop_reason not in ("error", "refusal")):
            return ToolResult.error(
                api.t("features.agent.aborted"),
                details={**details, "status": "aborted", "cancelled": True},
            )
        if last is None:
            details["error"] = api.report_error(
                api.t("features.agent.no_answer"),
                phase="subagent.result",
                store=owner,
                context={"agent": name, "role": "subagent"},
            )
            return failed(api.t("features.agent.no_answer"))
        if last.stop_reason in ("error", "refusal"):
            return failed(
                api.t(
                    "features.agent.failed",
                    error=safe_text(last.error_message or last.text() or last.stop_reason),
                )
            )
        if last.stop_reason == "aborted":
            return ToolResult.error(
                api.t("features.agent.aborted"),
                details={**details, "status": "aborted", "cancelled": True},
            )
        turns = sum(isinstance(m, AssistantMessage) for m in rt.messages)
        return ToolResult.text(
            last.text() or api.t("features.agent.no_text"), details={"agent": name, "turns": turns}
        )


def setup(api: ExtensionAPI) -> None:
    subs = Subagents(api)
    api.services["subagents"] = subs

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        prompt, desc = args["prompt"], args.get("description") or args["prompt"][:60]
        unknown = set(args) - {"description", "prompt", "read_only", "background"}
        if unknown:
            return ToolResult.error(f"Unexpected agent argument(s): {', '.join(sorted(unknown))}")
        read_only = args.get("read_only", False)
        jobs = api.services.get("jobs")
        runtime = api.runtime_for_context(ctx)
        if runtime is None and ctx.review_source is None and ctx.agent == "main":
            # Trusted direct main-tool integrations predate runtime-bound contexts.
            # Capture their origin now, before a background worker or UI switch.
            runtime = api.current_runtime()
        if runtime is None:
            return ToolResult.error("Subagent requires the originating runtime context.")
        review_parent = runtime.review_source
        review_parent.root.entries()

        def run_child(signal):
            return subs.run(prompt, desc, signal, ctx.cwd, review_parent, read_only=read_only)

        if args.get("background") and jobs is not None:
            job = jobs.start_callable(
                f"agent: {desc}",
                run_child,
                store=review_parent.root,
                agent=ctx.agent,
                tool_call_id=ctx.call_id,
                cwd=ctx.cwd,
            )
            if ctx.signal.is_set():
                # Abort may have arrived just before the job was registered.
                # Latch it before this tool returns and the parent can start a new run.
                jobs.kill(job)
            return ToolResult.text(ctx.t("features.agent.background", id=job.id, description=desc))
        return run_child(ctx.signal)

    api.register_tool(
        AgentTool(
            name="agent",
            label="Agent",
            description=(
                "Delegate a self-contained task to a sub-agent with a fresh context. It cannot see this "
                "conversation, so include the needed context; only its final answer comes back. Several calls in one "
                "response run in parallel. Set read_only=true to restrict tools and commands. "
                "Set background=true to keep working while it runs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Short task label"},
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Complete instructions for the sub-agent",
                    },
                    "read_only": {
                        "type": "boolean",
                        "description": "Limit the child to reading files and read-only shell commands.",
                    },
                    "background": {"type": "boolean"},
                },
                "required": ["description", "prompt"],
                "additionalProperties": False,
            },
            execute=execute,
            read_only=True,  # its own tool calls are permission-checked individually
            i18n_key="tools.agent",
        ),
        roles=("main",),
    )

