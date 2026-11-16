# Extending Virgo

[Documentation](../README.md) · [Core boundaries](core-plugin-boundaries.md) · [简体中文](../../zh-CN/development/extensions.md)

## Pick the smallest extension point

| Resource | User location | Project location | Capability |
| --- | --- | --- | --- |
| Python extensions | `~/.virgo/extensions/*.py` | `.virgo/extensions/*.py` | Trusted `setup(api)` code |
| Skills | `~/.virgo/skills/<name>/SKILL.md` | `.virgo/skills/<name>/SKILL.md`, then `.agents/skills/<name>/SKILL.md` | `skills` |
| Prompt templates | `~/.virgo/prompts/*.md` | `.virgo/prompts/*.md` | Core prompt assembly |
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

`execute(args, ctx)` returns `ToolResult` or a string. `ToolContext` supplies `cwd`, `call_id`, `signal`, `agent`, an optional progress callback and `origin`, the bound `ExecutionOrigin`. Preserve `ctx.origin` when adapting or deferring an operation; it identifies the caller even if another conversation is selected. Respect cancellation. `content` is model-visible; `details` is structured state/UI data. Return `ToolResult.error(...)` for a known failure, or let an exception retain its type and stack through Core diagnostics.

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
| `api.create_runtime(name, role, parent=..., read_only=True, ...)` | Construct a restricted child with inherited capability and approval ceilings, accounting, cancellation and shared request budget |
| `api.release_runtime(runtime)` | Release a completed child's registration and store failure-signal bindings |
| `api.reserve_child_work(store)` | Register pending agent work for its captured owner and return an idempotent release callback |
| `api.tools_for(role)`, `api.resolve(spec)` | Resolve permitted tool roles and model configuration |
| `api.authorize_tool(event)` | Apply the Core preflight/hooks/execution gate to an adapted operation |
| `api.notify(..., store=owner, diagnostics=...)` | Deliver asynchronous results to their originating conversation |
| `api.report_error(..., store=owner)`, `api.retain_diagnostics(owner, records)` | Preserve owner-bound diagnostic evidence |
| `api.complete(...)` | Make an accounted auxiliary model request using Core request policy |

`runtime_for_context` validates the real Runtime, owning Session, exact original `ExecutionOrigin` object and agent name. Invalid or foreign sources return `None`; fail the requested operation explicitly. Resolving a runtime does not grant access to unrelated diagnostic records. For tool calls, use this identity resolution rather than `current_runtime()`; the latter is for deliberately capturing the current conversation at entry time.

Pass the captured trusted parent to the child factory. `read_only=True` adds an execution ceiling without hiding tool schemas or selecting Plan; the child inherits a parent's read-only ceiling even if the argument is omitted or false. A child may also select `approval_policy="never"`; a parent's `never` ceiling cannot be loosened. The factory binds `origin_parent` to `parent.origin`, shares accounting and the request budget, and propagates cancellation. `budget.maxModelCalls`, when configured, limits model requests shared with children; `subagents.maxTurns` remains the focused child turn limit. Do not build an independent AgentSession inside a plugin to escape ownership or budgets.

Children capture their source permission profile before automatic project-context reads. The built-in background agent captures that profile when the task is submitted, so a later parent Mode change cannot widen a queued task. A trusted extension that defers Runtime creation can pass its captured `PermissionMode` through the `execution_mode` factory argument; this argument is not exposed in the model tool schema.

Use `api.services` only for an explicit shared service contract. The compatibility `api.session` escape hatch exists through the 0.2 series; official plugins use the facade. Do not mutate `_tools`, `_lock`, `main` or selected UI state. Retained `virgo.features` implementation imports are temporary aliases; migrate before 0.3. The former `virgo.features.permission*` imports have already been removed; use the Core APIs described here.

## Events and permissions

`api.on(event, handler, priority=0)` returns an unsubscribe callback. Higher priorities run first; ties preserve registration order. Important event contracts are:

| Event | Handler contract |
| --- | --- |
| `session_start`, `session_shutdown` | `(session)` |
| `input` | `(text, session)` → replacement text, `Handled`, or `None` |
| `user_steer` | `(text, session)` |
| `runtime_created`, `runtime_closed` | `(runtime)`; bind/release per-runtime UI and plugin state |
| `before_agent_start` | `(runtime, prompts)` |
| `agent_event`, `turn_end` | `(event, runtime)` |
| `tool_call` | `(event, session)` → `Block` or `None`; arguments may be adjusted |
| `tool_result` | `(result, event, session)` → replacement result or `None` |
| `context` | `(messages, runtime)` → request view or `None` |
| `agent_stop` | `(runtime)` → `Continue` or `None` |
| `agent_end`, `before_compact` | `(runtime)` |
| `compact` | `(runtime, summary)` |
| `work_mode_changed` | `(origin_runtime, mode)`; observe committed mode state |
| `proposed_plan_changed` | `(origin_runtime, proposal)`; observe the committed proposal |

Use `Block(reason)` to stop a tool and `Continue(text)` only for an explicitly enabled continuation policy. Handler failure is diagnosed and isolated from later handlers. Context views should preserve append-only message prefixes.

Before publishing queued agent work or starting its worker, call `api.reserve_child_work(owner)` with the captured originating accounting store. Release it on completion or startup failure, outside plugin locks and before signaling completion to callers. Registration and release use the same Core lock as mode admission. The reservation retains the originating Runtime for policy checks until its last pending job releases, including after conversation deletion. The official background service reserves callable agent jobs; subprocesses retain their launch policy.

Core determines whether the origin is busy from its own pending reservations and running runtimes, including descendants. Mode transitions and explicit plan execution require that origin to be idle; the former `runtime_pending_work` inspection hook is retired. State-change notifications run after persistence and state publication. Observers may refresh UI, but cannot synchronously change mode or plan, select a conversation, or admit new work. Start any subsequent explicit action through the normal admission path after notification returns.

`api.register_permission_policy(policy)` adds a hard restriction without replacing the mandatory Core authorization policy. Implement both `preflight(event)` and `before_execute(event)`, returning `Block(reason)` to deny or `None` to continue. At both checkpoints, Sandbox and additional policies run before Core evaluates capability ceilings, deny rules, existing grants and possible human approval. This prevents an approval question for an operation a hard boundary forbids. Core runs the second checkpoint after tool-call hooks and argument changes. Hosted/adapted operations must use `api.authorize_tool(event)` and carry the original caller context. `AgentTool.read_only` declares a trusted tool's capability; it neither grants all operations nor bypasses other policies. Python extensions themselves remain trusted code outside the Shell sandbox.

Core enforces deterministic authorization in `harness/permission_policy.py`. `harness/permissions.py` defines ask/never approval and read-only capability checks; `harness/origin_context.py` binds the Runtime, workspace and accounting branch. Model approval and `register_permission_reviewer` are retired. Plugins may add restrictions through `register_permission_policy`.

Settings command hooks support `PreToolUse`, `PostToolUse`, `UserPromptSubmit` and `Stop`; JSON is delivered on stdin. Exit `2` is the event's block/continue control result, with stderr feedback. Other failures are diagnosed. See [`extensions.py`](../../../src/virgo/harness/extensions.py) for matching and exact semantics.

## Prompt sections and state

`api.add_system_section(name, fn, order=50)` calls `fn(role)`. `add_runtime_system_section(name, fn, order=50)` supplies a candidate's `RuntimeContext` with `role`, `cwd`, `store`, `accounting_store` and `origin_parent`. The last field is the captured parent `ExecutionOrigin`, or `None` for a root. A construction failure rejects the candidate; do not temporarily swap active session state.

`api.add_request_system_section(name, fn, order=50)` calls `fn(runtime)` for every
request and compaction context. Return instructions or `None`. Use it for active
task instructions that must survive tool loops, compression and restoration.
It does not filter tools or change execution permissions. Plan and Code Review
use this hook; optional plugins own their task state and result handling.

`api.append_entry(kind, data)` and `api.entries(kind)` access custom state on the current store. Background work should capture its actual owner first, then use that store and `api.notify(..., store=owner)`. Save failure is a real execution boundary, not successful persistence. Todo remains conversation state, not a durable task graph.

## Skills and templates

Skills contain YAML frontmatter (`name`, `description`) followed by Markdown. Discovery exposes the index; the model reads content when needed. `/skill:name args` explicitly expands one. Relative references resolve from the skill directory.

A `prompts/inspect.md` template becomes `/inspect`:

```markdown
---
description: inspect the current changes
---
Inspect the current changes. Focus on: ${1:-correctness}. Additional context: $@
```

Templates support `$1`, `$@` / `$ARGUMENTS`, `${1:-value}` and `${@:2}`. Without placeholders, arguments append to the body.

When enabled, the [independent code-review plugin](../guides/code-review.md) owns `/review`. A `review.md` template causes a startup error and must be renamed; an extension cannot register another command under that name. There is no official `/code-review` alias. An `/inspect` prompt template uses the ordinary conversation and its tools, while `/review` runs an isolated review task.

Specialized instructions belong in a [Skill](../../../examples/project/.virgo/skills/code-review/SKILL.md) or delegated task prompt; create inspection children with `read_only=True`. Custom model or tool policies belong in trusted extensions. See [migration](../guides/migrating-to-core-plugins.md#subagent-capability-migration).

For caught exceptions use `virgo.diagnostics.capture_error` and retain structured errors in tool results; on successful recovery keep original evidence under `related_errors`. Prefer the original runtime's `report_error()` for runtime-owned evidence. Propagated results/notifications can grant parent access; an error-ID string alone cannot. See [Troubleshooting](../guides/troubleshooting.md).

Isolated commands use `api.register_command(..., isolated=True)` and `fn(session, args, origin_runtime)`. The Host binds the origin and owns busy/cancellation; create children with `parent=origin_runtime` and select `read_only=True` only when an explicit child ceiling is required. Review retains ordinary child tools, adds named snapshot readers and supplies reviewer instructions. Command-name duplicates are rejected; isolated command names are reserved against prompt templates, including `/review` while its plugin is enabled.
