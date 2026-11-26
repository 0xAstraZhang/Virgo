"""Sub-agents: delegate focused work with reusable persisted traces and inherited limits.

Children inherit the immutable execution context of their originating task.
Each sub-agent keeps its own trace store under the parent session so the main
conversation stays clean while users can inspect full traces and the lead agent
can reuse a sub-agent across related tasks.
"""

from __future__ import annotations

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import AssistantMessage, TextContent, ToolResultMessage, UserMessage
from ..diagnostics import safe_text
from ..harness.extensions import ExtensionAPI
from ..harness.preferences import effort_levels
from ..harness.subagents import SubagentBusyError, SubagentHandle, SubagentLookupError


class Subagents:
    def __init__(self, api: ExtensionAPI):
        self.api = api

    def _run_handle(
        self,
        handle: SubagentHandle,
        resumed: bool,
        prompt: str,
        description: str,
        signal,
        cwd,
        origin_parent,
        *,
        execution_context=None,
        parent_tool_call_id: str | None = None,
        job_id: str | None = None,
    ) -> ToolResult:
        api = self.api
        parent = origin_parent.runtime
        owner = origin_parent.root
        registry = api.subagent_store_for(owner)
        name = handle.id
        rt = None
        failure = cleanup_error = None
        final_status = "error"
        prompt_message = UserMessage(prompt)
        try:
            total_limit = api.settings.get("subagents.maxTotalTurns")
            remaining = total_limit - handle.assistant_turns()
            registry.record_run(
                handle,
                description=description,
                parent_tool_call_id=parent_tool_call_id,
                job_id=job_id,
            )
            if remaining > 0:
                effort = api.settings.get("subagents.effort")
                if effort is not None and effort not in effort_levels(api.model):
                    raise ValueError(f"Unsupported subagents.effort {effort!r} for {api.model.spec}")
                messages, ids = handle.store.build_context()
                rt = api.create_runtime(
                    name,
                    "subagent",
                    parent=parent,
                    cwd=cwd,
                    store=handle.store,
                    messages=messages,
                    ids=ids,
                    max_turns=min(int(api.settings.get("subagents.maxTurns", 20)), remaining),
                    effort=effort,
                    signal=signal,
                    compaction=True,
                    origin_parent=origin_parent,
                    execution_context=execution_context,
                    parent_tool_call_id=parent_tool_call_id,
                    description=description,
                    job_id=job_id,
                )
                rt.agent.prompt([prompt_message])
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

        # Only consider messages added during this run (or the compacted suffix if compaction occurred).
        if rt:
            start_index = next(
                (i + 1 for i in range(len(rt.messages) - 1, -1, -1) if rt.messages[i] is prompt_message),
                0,
            )
            new_messages = rt.messages[start_index:]
        else:
            new_messages = []
        last = (
            next((m for m in reversed(new_messages) if isinstance(m, AssistantMessage)), None)
            if new_messages
            else None
        )
        details: dict = {
            "agent": name,
            "agent_id": name,
            "resumed": resumed,
            "description": description,
        }
        if job_id is not None:
            details["job_id"] = job_id
        if failure is not None:
            details["error"] = failure
        elif last is not None and last.error_details is not None:
            details["error"] = last.error_details
        elif cleanup_error is not None:
            details["error"] = cleanup_error
        if cleanup_error is not None and cleanup_error != details.get("error"):
            details["related_errors"] = [cleanup_error]

        def failed(text: str) -> ToolResult:
            results = [message for message in new_messages if isinstance(message, ToolResultMessage)]
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
            return ToolResult.error(f"{text}\n\n[agent_id: {name}]", details=details)

        try:
            if failure is not None or cleanup_error is not None:
                return failed(api.t("features.agent.failed", error=details["error"]["message"]))
            cancelled = (signal is not None and signal.is_set()) or (rt is not None and rt.agent.signal.is_set())
            if cancelled and (last is None or last.stop_reason not in ("error", "refusal")):
                final_status = "aborted"
                return ToolResult.error(
                    f"{api.t('features.agent.aborted')}\n\n[agent_id: {name}]",
                    details={**details, "status": "aborted", "cancelled": True},
                )
            if remaining <= 0 or (last is not None and last.stop_reason == "turn_limit"):
                details["status"] = "turn_limit"
                if remaining <= 0 or remaining == rt.agent.cfg.max_turns:
                    return failed(api.t("features.agent.total_turn_limit", turns=total_limit))
                return failed(api.t("features.agent.turn_limit", turns=rt.agent.cfg.max_turns))
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
                final_status = "aborted"
                return ToolResult.error(
                    f"{api.t('features.agent.aborted')}\n\n[agent_id: {name}]",
                    details={**details, "status": "aborted", "cancelled": True},
                )
            if last.stop_reason != "stop":
                details["status"] = "incomplete"
                reason = safe_text(last.error_message or last.text() or last.stop_reason)[:500]
                return failed(api.t("features.agent.incomplete", reason=reason))
            answer = rt.agent.last_text
            if not answer.strip():
                details["status"] = "empty_answer"
                return failed(api.t("features.agent.no_answer"))
            turns = sum(isinstance(m, AssistantMessage) for m in new_messages)
            final_status = "done"
            return ToolResult.text(
                f"{answer}\n\n[agent_id: {name}]",
                details={**details, "turns": turns},
            )
        finally:
            registry.release(handle, status=final_status)

    def run(
        self,
        prompt: str,
        description: str,
        signal,
        cwd=None,
        origin_parent=None,
        *,
        execution_context=None,
        agent_id: str | None = None,
        parent_tool_call_id: str | None = None,
        job_id: str | None = None,
        handle: SubagentHandle | None = None,
        resumed: bool = False,
    ) -> ToolResult:
        """Run a new or reused child with the caller's authority and inherited limits."""
        api = self.api
        origin_parent = origin_parent or api.current_runtime().origin
        owner = origin_parent.root
        if handle is None:
            registry = api.subagent_store_for(owner)
            try:
                handle, resumed = registry.acquire(
                    agent_id,
                    description=description,
                    cwd=cwd or origin_parent.runtime.cwd,
                    parent_tool_call_id=parent_tool_call_id,
                    job_id=job_id,
                )
            except SubagentLookupError:
                return ToolResult.error(api.t("features.agent.unknown_id", id=agent_id))
            except SubagentBusyError:
                return ToolResult.error(api.t("features.agent.busy", id=agent_id))
        return self._run_handle(
            handle,
            resumed,
            prompt,
            description,
            signal,
            cwd,
            origin_parent,
            execution_context=execution_context,
            parent_tool_call_id=parent_tool_call_id,
            job_id=job_id,
        )


def setup(api: ExtensionAPI) -> None:
    subs = Subagents(api)
    api.services["subagents"] = subs

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        prompt, desc = args["prompt"], args.get("description") or args["prompt"][:60]
        raw_agent_id = args.get("agent_id")
        agent_id = raw_agent_id.strip() if isinstance(raw_agent_id, str) and raw_agent_id.strip() else None
        jobs = api.services.get("jobs")
        background_enabled = bool(jobs is not None and jobs.background_enabled)
        allowed_keys = (
            {"description", "prompt", "agent_id", "background"}
            if background_enabled
            else {"description", "prompt", "agent_id"}
        )
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

        if args.get("background"):
            if not background_enabled:
                return ToolResult.error(ctx.t("features.agent.background_disabled"))
            registry = api.subagent_store_for(origin_parent.root)
            try:
                handle, resumed = registry.acquire(
                    agent_id,
                    description=desc,
                    cwd=ctx.cwd,
                    parent_tool_call_id=ctx.call_id,
                )
            except SubagentLookupError:
                return ToolResult.error(ctx.t("features.agent.unknown_id", id=agent_id))
            except SubagentBusyError:
                return ToolResult.error(ctx.t("features.agent.busy", id=agent_id))

            job_ref: list[str | None] = [None]

            def run_background(signal):
                return subs.run(
                    prompt,
                    desc,
                    signal,
                    ctx.cwd,
                    origin_parent,
                    execution_context=execution_context,
                    parent_tool_call_id=ctx.call_id,
                    job_id=job_ref[0],
                    handle=handle,
                    resumed=resumed,
                )

            try:
                job = jobs.start_callable(
                    f"{handle.id}: {desc}",
                    run_background,
                    store=origin_parent.root,
                    agent=ctx.agent,
                    tool_call_id=ctx.call_id,
                    cwd=ctx.cwd,
                )
            except BaseException:
                registry.release(handle, status="error")
                raise
            job_ref[0] = job.id
            handle.job_id = job.id
            if ctx.signal.is_set():
                # Abort may have arrived just before the job was registered.
                # Latch it before this tool returns and the parent can start a new run.
                jobs.kill(job)
            return ToolResult.text(
                ctx.t("features.agent.background", id=job.id, agent_id=handle.id, description=desc),
                details={
                    "agent": handle.id,
                    "agent_id": handle.id,
                    "job_id": job.id,
                    "background": True,
                    "resumed": resumed,
                    "description": desc,
                },
            )

        return subs.run(
            prompt,
            desc,
            ctx.signal,
            ctx.cwd,
            origin_parent,
            execution_context=execution_context,
            agent_id=agent_id,
            parent_tool_call_id=ctx.call_id,
        )

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
            "agent_id": {
                "type": "string",
                "description": "Optional existing sub-agent ID from this session to reuse with its prior context.",
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

    def cmd_subagents(session, _args: str) -> None:
        items = session.subagent_store_for().list()
        if not items:
            session.ui.print(session.t("features.agent.list_empty"), "dim")
            return
        lines = []
        for item in items:
            status = session.t(f"ui.subagent.state.{item['status']}")
            desc = item["description"] or session.t("ui.subagent.no_description")
            lines.append(f"{item['id']}  [{status}]  {desc}")
        session.ui.print("\n".join(lines))

    api.register_command("subagents", cmd_subagents, "features.commands.subagents", concurrent=True)
    api.on("background_capability_changed", lambda _enabled: install_tools())
    subs.install_tools = install_tools
    install_tools()
