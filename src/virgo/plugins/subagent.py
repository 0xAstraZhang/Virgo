"""Sub-agents: delegate focused work with fresh contexts and inherited limits.

Children inherit the immutable execution context of their originating task.
"""

from __future__ import annotations

import itertools

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import AssistantMessage, TextContent, ToolResultMessage, UserMessage
from ..diagnostics import safe_text
from ..harness.extensions import ExtensionAPI

_counter = itertools.count(1)



class Subagents:
    def __init__(self, api: ExtensionAPI):
        self.api = api

    def run(
        self, prompt: str, description: str, signal, cwd=None, origin_parent=None,
        *, execution_context=None,
    ) -> ToolResult:
        """Run a child with the caller's authority and optional read-only limits."""
        api = self.api
        origin_parent = origin_parent or api.current_runtime().origin
        parent = origin_parent.runtime
        owner = origin_parent.root
        name = f"subagent#{next(_counter)}"
        rt = None
        failure = cleanup_error = None
        try:
            api.ui.print(f"[{name}] {description}", "agent")
            rt = api.create_runtime(
                name,
                "subagent",
                parent=parent,
                cwd=cwd,
                max_turns=int(api.settings.get("subagents.maxTurns", 50)),
                signal=signal,
                compaction=True,
                origin_parent=origin_parent,
                execution_context=execution_context,
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
                recent_messages = results[-8:]
                recent = [
                    {
                        "tool": message.tool_name,
                        "call_id": message.tool_call_id,
                        "outcome": "returned_error" if message.is_error else "returned_success",
                    }
                    for message in recent_messages
                ]
                # Pass a bounded reason for only the last three failures, never the
                # child's full tool transcript or the injected diagnostic capsule.
                shown = 0
                for message, item in zip(reversed(recent_messages), reversed(recent), strict=True):
                    if not message.is_error or shown >= 3:
                        continue
                    original = "\n".join(
                        block.text for block in message.content
                        if isinstance(block, TextContent)
                        and not block.text.startswith("[Diagnostic evidence; treat as data]")
                    )
                    if original.strip():
                        excerpt = " ".join(safe_text(original).split())
                        encoded = excerpt.encode("utf-8")
                        if len(encoded) > 320:
                            excerpt = "…" + encoded[-317:].decode("utf-8", errors="ignore")
                        item["reason"] = excerpt
                        shown += 1

                # Child diagnostics are runtime-scoped. Explicitly propagate the
                # latest failed tool records so the parent can inspect their IDs.
                related = list(details.get("related_errors", []))
                seen = {record.get("error_id") for record in related if isinstance(record, dict)}
                seen.add(details.get("error", {}).get("error_id"))
                for message in reversed(recent_messages):
                    if not message.is_error or not isinstance(message.details, dict):
                        continue
                    record = message.details.get("error")
                    if not isinstance(record, dict):
                        continue
                    error_id = record.get("error_id")
                    if isinstance(error_id, str) and error_id not in seen:
                        related.append(record)
                        seen.add(error_id)
                    if len(related) >= 3:
                        break
                if related:
                    details["related_errors"] = related

                details["child_progress"] = {
                    "completed_calls": sum(not message.is_error for message in results),
                    "recent_tools": recent,
                    "side_effects": "unknown",
                }
                text += api.t("features.agent.progress", count=len(results)) + "\n".join(
                    f"- {item['tool']} ({item['call_id']}): {item['outcome']}"
                    + (f" — {item['reason']}" if item.get("reason") else "")
                    for item in recent
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
        if last.stop_reason == "turn_limit":
            details["status"] = "turn_limit"
            return failed(api.t("features.agent.turn_limit", turns=rt.agent.cfg.max_turns))
        if last.stop_reason != "stop":
            details["status"] = "incomplete"
            reason = safe_text(last.error_message or last.text() or last.stop_reason)[:500]
            return failed(api.t("features.agent.incomplete", reason=reason))
        answer = rt.agent.last_text
        if not answer.strip():
            details["status"] = "empty_answer"
            return failed(api.t("features.agent.no_answer"))
        turns = sum(isinstance(m, AssistantMessage) for m in rt.messages)
        return ToolResult.text(answer, details={"agent": name, "turns": turns})


def setup(api: ExtensionAPI) -> None:
    subs = Subagents(api)
    api.services["subagents"] = subs

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        prompt, desc = args["prompt"], args.get("description") or args["prompt"][:60]
        jobs = api.services.get("jobs")
        background_enabled = bool(jobs is not None and jobs.background_enabled)
        allowed_keys = {"description", "prompt", "background"} if background_enabled else {"description", "prompt"}
        unknown = set(args) - allowed_keys
        if unknown:
            if unknown == {"background"} and not background_enabled:
                return ToolResult.error(ctx.t("features.agent.background_disabled"))
            return ToolResult.error(f"Unexpected agent argument(s): {', '.join(sorted(unknown))}")
        runtime = api.runtime_for_context(ctx)
        if runtime is None and ctx.origin is None and ctx.agent == "main":
            # Trusted direct main-tool integrations predate runtime-bound contexts.
            # Capture their origin now, before a background worker or UI switch.
            runtime = api.current_runtime()
        if runtime is None:
            return ToolResult.error("Subagent requires the originating runtime context.")
        origin_parent = runtime.origin
        origin_parent.root.entries()
        execution_context = runtime.execution_context

        def run_child(signal):
            return subs.run(prompt, desc, signal, ctx.cwd, origin_parent,
                            execution_context=execution_context)

        if args.get("background"):
            if not background_enabled:
                return ToolResult.error(ctx.t("features.agent.background_disabled"))
            job = jobs.start_callable(
                f"agent: {desc}",
                run_child,
                store=origin_parent.root,
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

    def install_tools() -> None:
        from ..i18n import Translator

        jobs = api.services.get("jobs")
        background_enabled = bool(jobs is not None and jobs.background_enabled)
        properties: dict = {
            "description": {"type": "string", "description": "Short task label"},
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "Complete instructions for the sub-agent",
            },
        }
        if background_enabled:
            properties["background"] = {
                "type": "boolean",
                "description": "Run in the background; job_output/job_kill manage the job.",
            }

        def values(t):
            return {"background": t("tools.agent.background") if background_enabled else ""}

        english = Translator()
        tool = AgentTool(
            execution_scope="delegate",
            name="agent",
            label="Agent",
            description=english("tools.agent.description", **values(english)),
            parameters={
                "type": "object",
                "properties": properties,
                "required": ["description", "prompt"],
                "additionalProperties": False,
            },
            execute=execute,
            side_effect="read",  # its own tool calls are permission-checked individually
            i18n_key="tools.agent",
            i18n_values=values,
        )
        api.replace_tools("tool:agent", [(tool, ("main",))])

    subs.install_tools = install_tools
    install_tools()

