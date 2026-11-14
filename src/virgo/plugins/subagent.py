"""Sub-agents: delegate focused work with a fresh context and inherited limits.

The public tool selects capabilities rather than agent types. Legacy named definitions
remain readable for old tool calls during the 0.2 compatibility window; they are not
advertised to the model. Legacy Markdown definitions live in
``~/.virgo/agents/`` or ``.virgo/agents/``::

    ---
    name: reviewer
    description: Reviews a diff for bugs
    tools: read, grep, glob, bash      # optional (default: all sub-agent tools)
    model: anthropic/claude-sonnet-5-5  # optional
    permission: plan                   # optional permission mode for this agent
    ---
    You are a meticulous code reviewer...
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import AssistantMessage, ToolResultMessage, UserMessage
from ..diagnostics import safe_text
from ..harness.commands import parse_frontmatter
from ..harness.extensions import ExtensionAPI

_counter = itertools.count(1)
READ_ONLY_TOOLS = frozenset({"read", "glob", "grep", "bash"})
READ_ONLY_PROMPT = "You are in read-only mode; file and system changes are not permitted."


@dataclass
class AgentType:
    name: str
    description: str
    prompt: str = ""
    tools: list[str] | None = None  # None = all sub-agent tools
    model: str | None = None
    permission: str | None = None
    source: str = "builtin"
    extra: dict = field(default_factory=dict)


BUILTIN_TYPES = [
    AgentType(
        "general",
        "General-purpose agent for multi-step tasks: research, edits, running commands.",
    ),
    AgentType(
        "explore",
        "Read-only agent for finding code and answering questions about the codebase.",
        prompt="You are in read-only mode; file and system changes are not permitted.",
        tools=["read", "glob", "grep", "bash"],
        permission="plan",
    ),
]


def load_types(api: ExtensionAPI) -> dict[str, AgentType]:
    types = {t.name: t for t in BUILTIN_TYPES}
    for d in api.paths.both("agents"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            tools = meta.get("tools")
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            name = str(meta.get("name") or f.stem)
            types[name] = AgentType(
                name=name,
                description=str(meta.get("description", "")).strip(),
                prompt=body.strip(),
                tools=tools,
                model=meta.get("model"),
                permission=meta.get("permission"),
                source=str(f),
            )
    return types


class Subagents:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.types = load_types(api)

    def run(
        self, prompt: str, agent_type: str | AgentType, description: str, signal, cwd=None, review_parent=None,
        *, read_only: bool = False,
    ) -> ToolResult:
        """Run a child with the caller's authority; named types are legacy overrides."""
        api = self.api
        review_parent = review_parent or api.current_runtime().review_source
        parent = review_parent.runtime
        owner = review_parent.root
        at = agent_type if isinstance(agent_type, AgentType) else self.types.get(agent_type)
        if at is None:
            return ToolResult.error(
                api.t("features.agent.unknown_type", type=agent_type, available=", ".join(self.types))
            )
        pool = api.tools_for("subagent")
        allowed = set(at.tools) if at.tools is not None else None
        if read_only:
            # A capability can only narrow an existing legacy tool allowlist.
            allowed = set(READ_ONLY_TOOLS) if allowed is None else allowed & READ_ONLY_TOOLS
        tools = [t for t in pool if t.name in allowed] if allowed is not None else None
        permission = "plan" if read_only else at.permission
        extra = at.prompt
        if read_only and READ_ONLY_PROMPT not in extra:
            extra = f"{READ_ONLY_PROMPT}\n\n{extra}".strip()
        name = f"subagent#{next(_counter)}"
        rt = None
        failure = cleanup_error = None
        try:
            api.ui.print(f"[{name}] {description}", "agent")
            rt = api.create_runtime(
                name,
                "subagent",
                parent=parent,
                permission=permission,
                cwd=cwd,
                tools=tools,
                system_extra=extra or None,
                model=api.resolve(at.model) if at.model else None,
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
        # Old tool calls (including calls from extensions) may still specify a
        # named type. It is deliberately absent from the new model-facing schema.
        type_name = args.get("type") or "general"
        read_only = args.get("read_only", False)
        if "type" in args and "read_only" in args and type_name in ("general", "explore"):
            if read_only != (type_name == "explore"):
                return ToolResult.error(api.t("features.agent.conflicting_type"))
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
            # Do not change the call signature for existing direct integrations.
            options = {"read_only": True} if read_only else {}
            return subs.run(prompt, type_name, desc, signal, ctx.cwd, review_parent, **options)

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
            },
            execute=execute,
            read_only=True,  # its own tool calls are permission-checked individually
            i18n_key="tools.agent",
        ),
        roles=("main",),
    )

