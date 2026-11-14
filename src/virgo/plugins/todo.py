"""TodoWrite: the agent's own checklist for the current session.

State lives in the tool result ``details`` so it is rebuilt from the session file on resume.
"""

from __future__ import annotations

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import ToolResultMessage
from ..harness.extensions import ExtensionAPI
from ..i18n import Translator

STATUSES = ("pending", "in_progress", "completed")
MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def render(todos: list[dict], t=None) -> str:
    t = t or Translator()
    if not todos:
        return t("features.todo.empty")
    done = sum(t["status"] == "completed" for t in todos)
    lines = [f"{MARK[t['status']]} {t['content']}" for t in todos]
    return "\n".join(lines) + t("features.todo.completed", done=done, total=len(todos))


def validate_todos(todos: list, t=None) -> list[dict]:
    translate = t or Translator()
    out = []
    for i, t in enumerate(todos, 1):
        content = str(t.get("content", "")).strip()
        status = str(t.get("status", "pending")).strip().lower()
        if not content:
            raise ValueError(translate("features.todo.content_empty", index=i))
        if status not in STATUSES:
            raise ValueError(translate("features.todo.invalid_status", index=i, statuses=STATUSES))
        out.append({"content": content, "status": status})
    return out


def setup(api: ExtensionAPI) -> None:
    state: dict = {"todos": []}

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            todos = validate_todos(args["todos"], ctx.t)
        except ValueError as e:
            return ToolResult.error(ctx.t("features.todo.not_updated", error=e))
        state["todos"] = todos
        return ToolResult.text(render(todos, ctx.t), details={"todos": todos})

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
            i18n_key="tools.todo_write",
        ),
        roles=("main",),
    )

    def restore(_session):
        state["todos"] = []
        for m in api.current_runtime().messages:
            if (
                isinstance(m, ToolResultMessage)
                and m.tool_name == "todo_write"
                and isinstance(m.details, dict)
            ):
                if "todos" in m.details:
                    state["todos"] = m.details["todos"]

    api.on("session_start", restore)
    api.register_command(
        "todos",
        lambda _s, _a: api.ui.print(render(state["todos"], api.t)),
        "show the todo list",
        concurrent=True,
        description_key="features.commands.todos",
    )
    api.services["todos"] = state
