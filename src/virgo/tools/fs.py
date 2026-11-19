"""Filesystem tool declarations and adapters to the common sandbox executor.

Model paths remain opaque strings in this module. Resolution, metadata probes,
reads, walks and atomic mutations happen in the trusted filesystem worker.
"""
from __future__ import annotations

from dataclasses import replace

from ..agent.types import AgentTool, ToolContext, ToolResult, image_result
from ..execution import (
    ExecutionCancelled,
    ExecutionContext,
    ExecutionFailed,
    ExecutionUnavailable,
    FileAccessDenied,
)
from .output import MAX_BYTES, MAX_LINES


def _render(value, ctx):
    if isinstance(value, str):
        return value
    return ctx.t(value["key"], **{key: _render(item, ctx) if isinstance(item, dict) else item
                                for key, item in value.get("values", {}).items()})


def _execute(operation, args, ctx, *, access=None):
    if access is None:
        access = ExecutionContext.unrestricted(ctx.cwd)
        try:
            return _execute(operation, args, ctx, access=access)
        finally:
            access.executor.close()
    mutation = operation in {"write", "edit"}
    try:
        response = access.run_file_operation(operation, args, cwd=ctx.cwd, cancellation=ctx.signal)
    except ExecutionCancelled as error:
        return ToolResult.error(str(error), details={"status": "aborted", "cancelled": True, "boundary": {
            **access.describe(), "kind": "aborted", "payload_started": False, "side_effects": "none",
            **getattr(error, "boundary", {}),
        }})
    except ExecutionUnavailable as error:
        return ToolResult.error(str(error), details={"status": "execution_unavailable", "boundary": {
            **access.describe(), "kind": "execution_unavailable", "reason": error.reason,
            "dimension": error.dimension, "target": error.target,
            "payload_started": False, "side_effects": "none", "escalation_available": False,
        }})
    except ExecutionFailed as error:
        return ToolResult.error(str(error), details={"status": "execution_failed", "boundary": {
            **access.describe(), "kind": "execution_failed", "payload_started": "unknown",
            "side_effects": "possible" if mutation else "none", "escalation_available": False,
            **getattr(error, "boundary", {}),
        }})
    except FileAccessDenied as error:
        return ToolResult.error(str(error), details={"status": "resource_denied", "boundary": {
            **access.describe(), "kind": "resource_denied", "dimension": error.dimension,
            "target": error.target, "payload_started": False, "side_effects": "none",
        }})
    text = "".join(_render(segment, ctx) for segment in response.get("segments", []))
    result = (image_result(response["image"]["data"], response["image"]["mime"], text)
              if response.get("image") else ToolResult.text(text, is_error=not response["ok"]))
    facts = {**access.describe(), "payload_started": True,
             "side_effects": "possible" if mutation else "none", **response.get("boundary", {})}
    details = dict(response.get("details") or {})
    if (error := response.get("error")) and error.get("kind") == "permission_denied":
        details["status"] = "resource_denied"
        facts.update(kind="resource_denied", dimension="write_paths" if mutation else "read_paths",
                     target=error.get("target"))
    elif error:
        details["status"] = "file_operation_failed"
    result.details = {**details, "boundary": facts}
    return result


def _read(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    return _execute("read", args, ctx, access=access)


def _write(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    return _execute("write", args, ctx, access=access)


def _edit(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    return _execute("edit", args, ctx, access=access)


def _glob(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    return _execute("glob", args, ctx, access=access)


def _grep(args: dict, ctx: ToolContext, *, access=None) -> ToolResult:
    return _execute("grep", args, ctx, access=access)


def _prepare_edit(args: dict) -> dict:
    import json

    a = dict(args)
    if isinstance(a.get("edits"), str):
        try:
            a["edits"] = json.loads(a["edits"])
        except json.JSONDecodeError:
            pass
    old = a.pop("old_text", a.pop("oldText", None))
    new = a.pop("new_text", a.pop("newText", None))
    if old is not None and "edits" not in a:
        a["edits"] = [{"old_text": old, "new_text": new or ""}]
    if isinstance(a.get("edits"), list):
        a["edits"] = [
            {
                "old_text": e.get("old_text", e.get("oldText")),
                "new_text": e.get("new_text", e.get("newText", "")),
            }
            if isinstance(e, dict)
            else e
            for e in a["edits"]
        ]
    return a


READ = AgentTool(
    name="read",
    label="Read",
    description=(
        f"Read a text file (or an image). Returns at most {MAX_LINES} lines / {MAX_BYTES // 1024}KB per call; "
        "use offset (1-based line number) and limit to page through larger files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, relative to the working directory or absolute",
            },
            "offset": {"type": "integer", "minimum": 1, "description": "1-based line to start from"},
            "limit": {"type": "integer", "minimum": 1, "description": "Maximum number of lines"},
        },
        "required": ["path"],
    },
    execute=_read,
    side_effect="read",
    i18n_key="tools.read",
)


WRITE = AgentTool(
    name="write",
    label="Write",
    description="Create or overwrite a file with the given content. Parent directories are created.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    execute=_write,
    i18n_key="tools.write",
)


EDIT = AgentTool(
    name="edit",
    label="Edit",
    description=(
        "Replace exact text in a file. Each edit's old_text must match exactly one place in the current file "
        "(include enough context to be unique); edits must not overlap. Several edits to one file can be sent "
        "in a single call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}},
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["path", "edits"],
    },
    execute=_edit,
    prepare_arguments=_prepare_edit,
    i18n_key="tools.edit",
)


GLOB = AgentTool(
    name="glob",
    label="Glob",
    description="Find files by glob pattern (e.g. '**/*.py', 'src/**/test_*.ts'), newest first. "
    "Skips .git, node_modules, virtualenvs and caches.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search"},
        },
        "required": ["pattern"],
    },
    execute=_glob,
    side_effect="read",
    i18n_key="tools.glob",
)


GREP = AgentTool(
    name="grep",
    label="Grep",
    description="Search file contents with a Python regular expression. Output lines are 'path:line:text'.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
            "ignore_case": {"type": "boolean"},
            "context": {"type": "integer", "minimum": 0, "description": "Lines of context around matches"},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["pattern"],
    },
    execute=_grep,
    side_effect="read",
    i18n_key="tools.grep",
)

def make_file_tool(tool: AgentTool, execution) -> AgentTool:
    """Bind the operation to its immutable originating task context."""
    def execute(args, ctx):
        return tool.execute(args, ctx, access=execution(ctx))

    return replace(tool, execute=execute, execution_scope="file")
