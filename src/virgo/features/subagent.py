"""Sub-agents: delegate a focused task to an agent with a fresh context; only its final answer returns.

Built-in types: ``general`` (all base tools) and ``explore`` (read-only). Custom types are Markdown
files in ``~/.virgo/agents/`` or ``.virgo/agents/``::

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
        self, prompt: str, agent_type: str | AgentType, description: str, signal, cwd=None, review_parent=None
    ) -> ToolResult:
        """Run ``agent_type``: a registered type name, or an ad-hoc AgentType (e.g. a workflow step)."""
        session = self.api.session
        review_parent = review_parent or session.main.review_source
        owner = review_parent.root
        at = agent_type if isinstance(agent_type, AgentType) else self.types.get(agent_type)
        if at is None:
            return ToolResult.error(
                session.t("features.agent.unknown_type", type=agent_type, available=", ".join(self.types))
            )
        pool = session.tools_for("subagent")
        tools = [t for t in pool if t.name in at.tools] if at.tools is not None else None
        name = f"{at.name}#{next(_counter)}"
        perms = session.services.get("permissions")
        if perms and at.permission:
            perms.set_agent_mode(name, at.permission)
        rt = None
        failure = cleanup_error = None
        try:
            session.ui.print(f"[{name}] {description}", "agent")
            rt = session.create_runtime(
                name,
                "subagent",
                cwd=cwd,
                tools=tools,
                system_extra=at.prompt or None,
                model=session.resolve(at.model) if at.model else None,
                max_turns=int(session.settings.get("subagents.maxTurns", 50)),
                signal=signal,
                compaction=True,
                review_parent=review_parent,
            )
            rt.agent.prompt([UserMessage(prompt)])
        except Exception as error:
            failure = session.report_error(
                error, phase="subagent.run", store=owner, context={"agent": name, "role": "subagent"}
            )
        finally:
            if perms and at.permission:
                try:
                    perms.set_agent_mode(name, None)
                except Exception as error:
                    cleanup_error = session.report_error(
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
                text += session.t("features.agent.progress", count=len(results)) + "\n".join(
                    f"- {item['tool']} ({item['call_id']}): {item['outcome']}" for item in recent
                )
            return ToolResult.error(text, details=details)

        if failure is not None or cleanup_error is not None:
            return failed(session.t("features.agent.failed", error=details["error"]["message"]))
        cancelled = (signal is not None and signal.is_set()) or (rt is not None and rt.agent.signal.is_set())
        if cancelled and (last is None or last.stop_reason not in ("error", "refusal")):
            return ToolResult.error(
                session.t("features.agent.aborted"),
                details={**details, "status": "aborted", "cancelled": True},
            )
        if last is None:
            details["error"] = session.report_error(
                session.t("features.agent.no_answer"),
                phase="subagent.result",
                store=owner,
                context={"agent": name, "role": "subagent"},
            )
            return failed(session.t("features.agent.no_answer"))
        if last.stop_reason in ("error", "refusal"):
            return failed(
                session.t(
                    "features.agent.failed",
                    error=safe_text(last.error_message or last.text() or last.stop_reason),
                )
            )
        if last.stop_reason == "aborted":
            return ToolResult.error(
                session.t("features.agent.aborted"),
                details={**details, "status": "aborted", "cancelled": True},
            )
        turns = sum(isinstance(m, AssistantMessage) for m in rt.messages)
        return ToolResult.text(
            last.text() or session.t("features.agent.no_text"), details={"agent": name, "turns": turns}
        )


def setup(api: ExtensionAPI) -> None:
    subs = Subagents(api)
    api.services["subagents"] = subs

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        prompt, desc = args["prompt"], args.get("description") or args["prompt"][:60]
        type_name = args.get("type") or "general"
        jobs = api.services.get("jobs")
        review_parent = ctx.review_source or api.session.main.review_source
        review_parent.root.entries()
        if args.get("background") and jobs is not None:
            job = jobs.start_callable(
                f"agent: {desc}",
                lambda signal: subs.run(prompt, type_name, desc, signal, ctx.cwd, review_parent),
                store=review_parent.root,
                agent=ctx.agent,
                tool_call_id=ctx.call_id,
                cwd=ctx.cwd,
            )
            return ToolResult.text(ctx.t("features.agent.background", id=job.id, description=desc))
        return subs.run(prompt, type_name, desc, ctx.signal, ctx.cwd, review_parent)

    api.register_tool(
        AgentTool(
            name="agent",
            label="Agent",
            description=(
                "Delegate a self-contained task to a sub-agent with a fresh context. It cannot see this "
                "conversation, so include the needed context; only its final answer comes back. Several calls in one "
                "response run in parallel. Set background=true to keep working while it runs."
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
                    "type": {"type": "string", "description": "Agent type (default: general)"},
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

    def section(role: str):
        if role != "main":
            return None
        lines = [
            f"- {t.name}: {api.session.t('features.agent.type.' + t.name) if t.source == 'builtin' else t.description}"
            for t in subs.types.values()
        ]
        return "<agent_types>\nSub-agent types for the agent tool:\n" + "\n".join(lines) + "\n</agent_types>"

    api.add_system_section("agent_types", section, order=30)
