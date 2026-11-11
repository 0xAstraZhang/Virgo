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
from ..ai.types import AssistantMessage, UserMessage
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
        "Fast read-only agent for finding code and answering questions about the codebase.",
        prompt=(
            "You are in read-only mode: search and read, do not modify files. Be efficient: use glob/grep to "
            "locate, read only what is needed, and report concrete paths, line numbers and findings."
        ),
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

    def run(self, prompt: str, type_name: str, description: str, signal, cwd=None, review_parent=None) -> ToolResult:
        from ..harness.session import Runtime

        session = self.api.session
        at = self.types.get(type_name)
        if at is None:
            return ToolResult.error(f"Unknown agent type {type_name!r}. Available: {', '.join(self.types)}")
        pool = session.tools_for("subagent")
        tools = [t for t in pool if at.tools is None or t.name in at.tools]
        name = f"{at.name}#{next(_counter)}"
        perms = session.services.get("permissions")
        if perms and at.permission:
            perms.set_agent_mode(name, at.permission)
        session.ui.print(f"[{name}] {description}", "agent")
        try:
            rt = Runtime(
                session,
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
        finally:
            if perms and at.permission:
                perms.set_agent_mode(name, None)
        last = next((m for m in reversed(rt.messages) if isinstance(m, AssistantMessage)), None)
        if last is None:
            return ToolResult.error("The sub-agent produced no answer.")
        if last.stop_reason in ("error", "refusal"):
            return ToolResult.error(f"Sub-agent failed: {last.error_message}")
        if last.stop_reason == "aborted":
            return ToolResult.error("Sub-agent was aborted.")
        turns = sum(isinstance(m, AssistantMessage) for m in rt.messages)
        return ToolResult.text(
            last.text() or "(no text in final answer)", details={"agent": name, "turns": turns}
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
                f"agent: {desc}", lambda signal: subs.run(prompt, type_name, desc, signal, ctx.cwd, review_parent)
            )
            return ToolResult.text(
                f"Started background agent {job.id} ({desc}). You will be notified when it finishes; "
                "use job_output to read its result."
            )
        return subs.run(prompt, type_name, desc, ctx.signal, ctx.cwd, review_parent)

    api.register_tool(
        AgentTool(
            name="agent",
            label="Agent",
            description=(
                "Delegate a self-contained task to a sub-agent with a fresh context. It cannot see this "
                "conversation, so the prompt must contain everything it needs; only its final answer comes back. "
                "Use it to keep large searches or side quests out of your context. Several agent calls in one "
                "response run in parallel. Set background=true to keep working while it runs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "3-6 word label for the task"},
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
            snippet="delegate a focused task to a sub-agent (fresh context, returns its final answer)",
            read_only=True,  # its own tool calls are permission-checked individually
        ),
        roles=("main",),
    )

    def section(role: str):
        if role != "main":
            return None
        lines = [f"- {t.name}: {t.description}" for t in subs.types.values()]
        return "<agent_types>\nSub-agent types for the agent tool:\n" + "\n".join(lines) + "\n</agent_types>"

    api.add_system_section("agent_types", section, order=30)
