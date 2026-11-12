"""TodoWrite: the agent's own checklist for the current session.

State lives in the tool result ``details`` so it is rebuilt from the session file on resume.
"""

from __future__ import annotations

from ..agent.types import AgentTool, ToolContext, ToolResult
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
    return out


def setup(api: ExtensionAPI) -> None:
    state: dict = {"todos": []}

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
                "Replace the session checklist with the complete supplied list. "
                "Each item is pending, in_progress or completed; multiple items may be in_progress."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
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

    api.on("session_start", restore)
    api.register_command(
        "todos", lambda s, a: api.ui.print(render(state["todos"])), "show the todo list", concurrent=True
    )
    api.services["todos"] = state
