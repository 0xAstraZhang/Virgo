"""TodoWrite: the agent's own checklist for the current session.

State lives in the tool result ``details`` so it is rebuilt from the session file on resume.
When the agent tries to finish with unfinished items, it is reminded once per request.
"""

from __future__ import annotations

from ..agent.types import AgentTool, Continue, ToolContext, ToolResult
from ..ai.types import ToolResultMessage
from ..harness.extensions import ExtensionAPI

STATUSES = ("pending", "in_progress", "completed")
MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def render(todos: list[dict]) -> str:
    if not todos:
        return "(todo list is empty)"
    done = sum(t["status"] == "completed" for t in todos)
    lines = [f"{MARK[t['status']]} {t['content']}" for t in todos]
    return "\n".join(lines) + f"\n({done}/{len(todos)} completed)"


def validate_todos(todos: list) -> list[dict]:
    out = []
    for i, t in enumerate(todos, 1):
        content = str(t.get("content", "")).strip()
        status = str(t.get("status", "pending")).strip().lower()
        if not content:
            raise ValueError(f"item {i}: content is empty")
        if status not in STATUSES:
            raise ValueError(f"item {i}: status must be one of {STATUSES}")
        out.append({"content": content, "status": status})
    if sum(t["status"] == "in_progress" for t in out) > 1:
        raise ValueError("only one item can be in_progress at a time")
    return out


def setup(api: ExtensionAPI) -> None:
    state: dict = {"todos": [], "reminded": False}

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            todos = validate_todos(args["todos"])
        except ValueError as e:
            return ToolResult.error(f"Todo list not updated: {e}")
        state["todos"] = todos
        return ToolResult.text(render(todos), details={"todos": todos})

    api.register_tool(
        AgentTool(
            name="todo_write",
            label="Todos",
            description=(
                "Maintain your checklist for multi-step work. Send the complete list every time (it replaces the "
                "previous one). Use it for tasks with 3+ steps: plan first, keep exactly one item in_progress while "
                "working, and mark items completed as soon as they are done."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "minLength": 1},
                                "status": {"type": "string", "enum": list(STATUSES)},
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
            execute=execute,
            snippet="plan and track multi-step work as a checklist",
            read_only=True,
        ),
        roles=("main",),
    )

    def restore(session):
        state["todos"] = []
        for m in session.main.messages:
            if (
                isinstance(m, ToolResultMessage)
                and m.tool_name == "todo_write"
                and isinstance(m.details, dict)
            ):
                if "todos" in m.details:
                    state["todos"] = m.details["todos"]

    def reset_reminder(rt, prompts):
        if any(getattr(p, "kind", None) is None for p in prompts):
            state["reminded"] = False

    def on_stop(rt):
        if rt.role != "main" or state["reminded"]:
            return None
        open_items = [t for t in state["todos"] if t["status"] != "completed"]
        if not open_items:
            return None
        state["reminded"] = True
        listing = "\n".join(f"- [{t['status']}] {t['content']}" for t in open_items)
        return Continue(
            "<reminder>\nYour todo list still has unfinished items:\n"
            f"{listing}\nContinue working on them, or update the list with todo_write if they are done or no "
            "longer needed. If you need input from the user, ask with ask_user, or say so and stop.\n</reminder>"
        )

    api.on("session_start", restore)
    api.on("before_agent_start", reset_reminder)
    api.on("agent_stop", on_stop, priority=10)
    api.register_command(
        "todos", lambda s, a: api.ui.print(render(state["todos"])), "show the todo list", concurrent=True
    )
    api.services["todos"] = state
