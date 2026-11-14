# Extending Virgo

[Documentation](../README.md) · [Core boundaries](core-plugin-boundaries.md) · [简体中文](../../zh-CN/development/extensions.md)

## Pick the smallest extension point

| Resource | User location | Project location | Capability |
| --- | --- | --- | --- |
| Python extensions | `~/.virgo/extensions/*.py` | `.virgo/extensions/*.py` | Trusted `setup(api)` code |
| Skills | `~/.virgo/skills/<name>/SKILL.md` | `.virgo/skills/<name>/SKILL.md`, then `.agents/skills/<name>/SKILL.md` | `skills` |
| Prompt templates | `~/.virgo/prompts/*.md` | `.virgo/prompts/*.md` | Core prompt assembly |
| Subagent types | `~/.virgo/agents/*.md` | `.virgo/agents/*.md` | `subagent` |
| MCP servers | `~/.virgo/mcp.json` | `.virgo/mcp.json` | `mcp` |

`VIRGO_HOME` replaces the user directory. Declarative resources load user-first, then project; later definitions of the same name win. Official plugins live in `virgo.plugins` and use the same `setup(api)` interface as Python extensions. Plugins are loaded once in a fixed order; restart or initialize a workspace session to rediscover files. There is no live unload, dependency installer or plugin sandbox.

Project Python extensions require explicit trust outside project settings: `--trust-project-extensions` for the selected project, or user-level `extensions.trustedProjects`. Review the code before granting trust. User extensions are user-controlled trusted code. `--no-extensions` disables both Python extension locations without disabling official plugins. `minimal` selects no official capabilities, but trusted Python extensions are separate; use `--profile minimal --no-extensions` for neither. See [Migration](../guides/migrating-to-core-plugins.md#trust-project-extensions).

## Add a tool and command

Create `.virgo/extensions/project_overview.py`, review it, then start with `--trust-project-extensions`:

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
            description="List names directly inside the working directory.",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=overview,
            read_only=True,
        ),
        roles=("main", "subagent"),
    )
    api.register_command(
        "location", lambda _session, _args: api.ui.print(str(api.cwd)),
        "show the working directory", concurrent=True,
    )
```

`execute(args, ctx)` returns `ToolResult` or a string. `ToolContext` supplies `cwd`, `call_id`, `signal`, `agent`, an optional progress callback and bound review source. Respect cancellation. `content` is model-visible; `details` is structured state/UI data. Return `ToolResult.error(...)` for a known failure, or let an exception retain its type and stack through Core diagnostics.

`AgentTool` supports `sequential`, `prepare_arguments` and localization metadata. Tool names, argument keys and enum values are protocol contracts and must not be translated. See [Localization](localization.md) for `i18n_key`, `api.t` and `ctx.t`.

Commands retain the `(session, args: str)` callback signature for compatibility; new code can use the captured API rather than Session internals. Use `concurrent=True` only for safe operations during an active run. `argument_hint="[path]"` supplies an editor hint; `description_key` and `argument_hint_key` support localization.

## Registration, publication and cleanup

Every tool, command and event handler has a plugin owner. Duplicate tool/command names fail, including collisions with Core and another plugin. Prompt sections are scoped by owner. A failed optional setup is diagnosed and its partial registrations are rolled back, rather than leaving a half-loaded capability. Core initialization or permission failure blocks execution.

`api.replace_tools(namespace, [(tool, roles), ...])` atomically replaces one owned dynamic set after validating the full candidate. It cannot claim another owner's namespace or overwrite another owner's tools. Existing requests and running batches retain their snapshot; each runtime sees new schemas at its next model request boundary. A transport-specific stale handle can fail conservatively without replaying side effects. MCP uses this API instead of modifying `session._tools`.

Register resource cleanup immediately after acquiring the resource:

```python
def setup(api):
    connection = open_connection()  # supplied by this extension
    api.on_cleanup(connection.close)
    # Register tools that use connection only after cleanup is registered.
```

`on_cleanup(callback)` runs callbacks in reverse order during shutdown or failed setup rollback. Cleanup exceptions are diagnosed and do not stop the remaining callbacks. Keep shutdown idempotent and bounded; respect the cancellation signals of work you create.

## Runtime and diagnostic services

| Public API | Use |
| --- | --- |
| `api.settings`, `api.paths`, `api.cwd`, `api.ui`, `api.t` | Effective configuration, resource paths and host services |
| `api.current_runtime()`, `api.current_store()` | Capture the current owner at command/initialization time |
| `api.runtime_for_context(ctx)` | Resolve a tool caller without borrowing the currently selected conversation |
| `api.create_runtime(name, role, parent=..., permission=..., ...)` | Construct a child with inherited permission ceiling, accounting, cancellation and shared request budget |
| `api.release_runtime(runtime)` | Release a completed child's registration and associated policy state |
| `api.tools_for(role)`, `api.resolve(spec)` | Resolve permitted tool roles and model configuration |
| `api.authorize_tool(event)` | Apply the Core preflight/hooks/execution gate to an adapted operation |
| `api.notify(..., store=owner, diagnostics=...)` | Deliver asynchronous results to their originating conversation |
| `api.report_error(..., store=owner)`, `api.retain_diagnostics(owner, records)` | Preserve owner-bound diagnostic evidence |
| `api.complete(...)` | Make an accounted auxiliary model request using Core request policy |

`runtime_for_context` validates the real Runtime, owning Session, original review-source identity and agent name. Invalid or foreign sources return `None`; fail the requested operation explicitly. Resolving a runtime does not grant access to unrelated diagnostic records. For tool calls, use this identity resolution rather than `current_runtime()`; the latter is for deliberately capturing the current conversation at entry time.

The child factory takes an explicit trusted parent and prevents a child from increasing that parent's permission level. `budget.maxModelCalls`, when configured, limits model requests shared with children; `subagents.maxTurns` remains the focused child turn limit. Do not build an independent AgentSession inside a plugin to escape ownership or budgets.

Use `api.services` only for an explicit shared service contract. The compatibility `api.session` escape hatch exists through the 0.2 series; official plugins use the facade. Do not mutate `_tools`, `_lock`, `main` or selected UI state. Retained `virgo.features` implementation imports are also temporary aliases; migrate before 0.3.

## Events and permissions

`api.on(event, handler, priority=0)` returns an unsubscribe callback. Higher priorities run first; ties preserve registration order. Existing event signatures remain:

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

Use `Block(reason)` to stop a tool and `Continue(text)` only for an explicitly enabled continuation policy. Handler failure is diagnosed and isolated from later handlers. Context views should preserve append-only message prefixes.

`api.register_permission_policy(policy)` adds restrictions after the mandatory Core policy; it cannot replace the Core gate. A policy implements both `preflight(event)` and `before_execute(event)`. The second checkpoint catches changes after preflight. Hosted/adapted operations must use `api.authorize_tool`; a truthy `read_only` flag is not authorization. Python itself remains trusted code, not sandboxed execution.

Permission modes and helpers live in `virgo.harness.permissions`; concrete guards, scope detection and review-source contracts also belong to the harness. `auto_review` supplies an optional reviewer through `register_permission_reviewer`. Disabling it retains rules and human approval for uncertain operations.

Settings command hooks support `PreToolUse`, `PostToolUse`, `UserPromptSubmit` and `Stop`; JSON is delivered on stdin. Exit `2` is the event's block/continue control result, with stderr feedback. Other failures are diagnosed. See [`extensions.py`](../../../src/virgo/harness/extensions.py) for matching and exact semantics.

## Prompt sections and state

`api.add_system_section(name, fn, order=50)` calls `fn(role)`. `add_runtime_system_section(name, fn, order=50)` supplies a candidate's `RuntimeContext` with role, cwd, store, accounting store and review parent. A construction failure rejects the candidate; do not temporarily swap active session state.

`api.append_entry(kind, data)` and `api.entries(kind)` access custom state on the current store. Background work should capture its actual owner first, then use that store and `api.notify(..., store=owner)`. Save failure is a real execution boundary, not successful persistence. Todo remains conversation state, not a durable task graph.

## Skills, templates and agent types

Skills contain YAML frontmatter (`name`, `description`) followed by Markdown. Discovery exposes the index; the model reads content when needed. `/skill:name args` explicitly expands one. Relative references resolve from the skill directory.

A `prompts/review.md` template becomes `/review`:

```markdown
---
description: review the current diff
---
Review the current diff. Focus on: ${1:-correctness}. Additional context: $@
```

Templates support `$1`, `$@` / `$ARGUMENTS`, `${1:-value}` and `${@:2}`. Without placeholders, arguments append to the body. Subagent frontmatter supports `name`, `description`, `tools`, `model` and `permission`; its body supplies additional system instructions. The [reviewer example](../../../examples/project/.virgo/agents/reviewer.md) selects read-only tools and plan mode.

For caught exceptions use `virgo.diagnostics.capture_error` and retain structured errors in tool results; on successful recovery keep original evidence under `related_errors`. Prefer the original runtime's `report_error()` for runtime-owned evidence. Propagated results/notifications can grant parent access; an error-ID string alone cannot. See [Troubleshooting](../guides/troubleshooting.md).
