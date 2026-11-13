# Extending Virgo

[Documentation](../README.md) · [简体中文](../../zh-CN/development/extensions.md)

## Pick the smallest extension point

| Resource | User location | Project location |
| --- | --- | --- |
| Python extensions | `~/.virgo/extensions/*.py` | `.virgo/extensions/*.py` |
| Skills | `~/.virgo/skills/<name>/SKILL.md` | `.virgo/skills/<name>/SKILL.md`, then `.agents/skills/<name>/SKILL.md` |
| Prompt templates | `~/.virgo/prompts/*.md` | `.virgo/prompts/*.md` |
| Subagent types | `~/.virgo/agents/*.md` | `.virgo/agents/*.md` |
| Workflows | `~/.virgo/workflows/*.py` | `.virgo/workflows/*.py` |
| MCP servers | `~/.virgo/mcp.json` | `.virgo/mcp.json` |

`VIRGO_HOME` replaces the user directory. Resources load user-first, then project; same-named declarative resources use the later definition. Python extensions execute in load order. Restart or initialize a workspace session to rediscover files. Extensions are trusted Python code running inside Virgo's process.

## Add a tool and command

Create `.virgo/extensions/project_overview.py`:

```python
from virgo.agent import AgentTool, ToolResult


def setup(api):
    def overview(args, ctx):
        names = sorted(path.name for path in ctx.cwd.iterdir())
        return ToolResult.text("\n".join(names))

    api.register_tool(
        AgentTool(
            name="project_overview",
            label="Project overview",
            description="List the names directly inside the working directory.",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=overview,
            read_only=True,
        ),
        roles=("main", "subagent"),
    )

    def location(session, args):
        api.ui.print(str(session.cwd))

    api.register_command(
        "location", location, "show the working directory", concurrent=True
    )
```

`execute(args, ctx)` returns `ToolResult` or a string. `ToolContext` supplies `cwd`, `call_id`, `signal`, `agent`, optional progress callback and bound review source. Respect cancellation. `content` is model-visible; `details` is structured state/UI data. Use `ToolResult.error(...)` for a known failure, or let an exception retain its type and stack through the harness.

`AgentTool` also supports `sequential`, `prepare_arguments`, and localization metadata. Tool names and schemas are the public contract; do not translate names, property keys, enum values or executable examples. See [Localization](localization.md) for `i18n_key`, `api.t` and `ctx.t`.

Commands receive `(session, args: str)`. Use `concurrent=True` only for commands that safely read state while the agent is busy. A separate `argument_hint="[path]"` supplies the editor hint; do not derive it by parsing prose. Extensions can also supply `description_key` and `argument_hint_key` for localized metadata.

## Events and policies

`api.on(event, handler, priority=0)` registers a handler and returns an unsubscribe callback. Higher priorities run first; ties preserve registration order. Useful events include:

| Event | Handler contract |
| --- | --- |
| `session_start`, `session_shutdown` | `(session)` |
| `input` | `(text, session)` → replacement text, `Handled`, or `None` |
| `user_steer` | `(text, session)` |
| `before_agent_start` | `(runtime, prompts)` |
| `agent_event`, `turn_end` | `(event, runtime)` |
| `tool_call` | `(event, session)` → `Block` or `None`; arguments may be adjusted |
| `tool_result` | `(result, event, session)` → replacement result or `None` |
| `context` | `(messages, runtime)` → request view or `None` |
| `agent_stop` | `(runtime)` → `Continue` or `None` |
| `agent_end`, `before_compact` | `(runtime)` |
| `compact` | `(runtime, summary)` |

Use `Block(reason)` to stop a tool and `Continue(text)` for an explicitly enabled continuation policy. A handler failure is diagnosed and isolated from later handlers. Context views should keep old message prefixes stable.

A permission policy is registered with `api.register_permission_policy(policy)` and implements `preflight(event)` plus `before_execute(event)`. The second check is required because parameters or authorization can change after preflight. Use runtime-bound review sources; do not select authorization history from whichever conversation happens to be visible.

Command hooks in settings support `PreToolUse`, `PostToolUse`, `UserPromptSubmit` and `Stop`. They receive JSON on stdin. Exit code `2` is a control result (block/continue, depending on the event), with stderr used as feedback; ordinary failures are diagnosed. Hook matching and exact result semantics live in [`extensions.py`](../../../src/virgo/harness/extensions.py).

## Prompt sections and persistence

`api.add_system_section(name, fn, order=50)` calls `fn(role)`. For data tied to a candidate runtime, use `add_runtime_system_section(name, fn, order=50)`: its `RuntimeContext` exposes role, cwd, store, accounting store and review parent. Do not temporarily swap active session state during construction.

`api.append_entry(kind, data)` stores custom state; `api.entries(kind)` restores it from the active branch. Save failure is a real execution boundary, not a successful append. Background callbacks should capture their originating `store` and call `api.notify(..., store=owner)` or `session.report_error(..., store=owner)` so switching conversations cannot redirect their output.

## Skills, templates and agent types

A skill has YAML frontmatter with `name` and `description`, followed by Markdown instructions. Discovery exposes only its name, description and location; the model reads the body when needed. `/skill:name args` explicitly expands it. Relative references are resolved from the skill directory.

A prompt file such as `prompts/review.md` becomes `/review`:

```markdown
---
description: review the current diff
---
Review the current diff. Focus on: ${1:-correctness}. Additional context: $@
```

Templates support positional `$1`, all arguments `$@` / `$ARGUMENTS`, defaults `${1:-value}`, and slices `${@:2}`. Without placeholders, supplied arguments append to the template body.

Subagent Markdown frontmatter supports `name`, `description`, `tools`, `model` and `permission`; the body is its extra system prompt. The [reviewer example](../../../examples/project/.virgo/agents/reviewer.md) uses plan mode and an explicit read-only tool selection.

## Workflows

A workflow exports `META` and `run(ctx, args)`. `ctx.agent(prompt, schema=..., tools=..., label=...)` runs a child agent; `ctx.map` and `ctx.parallel` coordinate work, and `ctx.phase` / `ctx.log` report progress. Structured results are validated against the supplied schema.

The journal caches successful agent-call results by their call content. Resume reruns Python control flow from the top; effects outside journaled calls must be made repeatable or explicitly guarded. Workflow metadata is discovered once per workspace initialization, while execution reloads the chosen script. The [review workflow](../../../examples/project/.virgo/workflows/review-changes.py) demonstrates parallel review followed by verification.

## Errors and evidence

For a caught exception, `virgo.diagnostics.capture_error` preserves a structured diagnostic; return it under `ToolResult.details["error"]`. If recovering successfully, preserve the original under `related_errors`. Keep diagnostic context to IDs, counts and safe metadata, and do not attach arbitrary request objects.

Use the original runtime's `report_error()` when evidence belongs to it. Explicit result/notification propagation grants the parent evidence; sharing an error ID string alone does not. Details are in [Troubleshooting](../guides/troubleshooting.md) and the [extension API source](../../../src/virgo/harness/extensions.py).
