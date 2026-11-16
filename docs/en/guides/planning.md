# Plan a change before executing it

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/planning.md)

Plan is an optional task plugin. It adds investigation and planning instructions,
keeps one versioned Markdown proposal, and lets the user select a version for
implementation. It uses the existing Runtime and Agent Loop.

Plan does not select a permission preset, hide normal tools, or impose a read-only
execution ceiling. The model is instructed to investigate and propose changes;
the selected permission preset determines what its tools can actually do. Select
Read-Only separately when project writes must be prevented. A writable permission preset does
not make the planning task an instruction to implement its proposal.

## Start and revise a plan

The `coding` preset enables `plan`. To enable it in the minimal preset:

```json
{"profile": "minimal", "plugins": {"plan": {"enabled": true}}}
```

Use either the direct Plan command or a startup selection followed by a message:

```text
/plan Investigate the configuration loader and plan an optional timeout.
/plan
Investigate the configuration loader and propose an optional timeout.
/plan show
```

`/plan` without arguments selects planning instructions and displays the current
proposal when present. `/plan off` leaves the planning task without
executing a proposal or changing permissions. `/permissions` selects permissions; it is
not the Plan selector.

The instructions ask the model to inspect project instructions, relevant code,
existing tests and behavior before deciding. With `ask_user` available, it should
ask only about decisions that investigation cannot resolve and that materially
change scope, approach, behavior or acceptance. Unanswered questions remain
unresolved decisions or explicit assumptions.

The main Runtime publishes the complete Markdown through `propose_plan`. Include
a title, goal, change scope, key decisions, implementation steps, acceptance
criteria and known constraints. The tool records conversation metadata; it does
not create a project file or authorize execution. Request a revision in the
conversation, then use `/plan show` to inspect the new version.

The normal main tool set remains visible in every workflow, including
`propose_plan`, file tools, the selected Shell, and enabled plugin tools. Execution
permission checks still apply. Read-only child runtimes also retain their normal
tool definitions; the execution policy enforces their explicit ceiling.

## Edit and execute a selected version

Ctrl+G opens the proposal editor when the Plan plugin is enabled. Saving replaces
the full Markdown and creates a new revision. Dirty edits cannot be executed; an
outdated editor cannot overwrite or execute a newer proposal. Closing discards
unsaved editor changes.

The command equivalents are:

```text
/plan edit
/plan edit <plan-id>:<revision>
<complete replacement Markdown>
/plan execute <plan-id>:<revision>
```

For a conditional edit, send the token, newline and complete replacement body in
one command. The interactive edit path also checks the version it opened.

Execution requires the exact current token. A missing, stale or foreign token is
rejected. The user command selects that version for one run, changes the task
instructions to Default, and starts the existing Runtime. The selected Markdown
appears once in its current request context. The permission preset and approval
policy remain unchanged. If a required operation exceeds those permissions, the
normal failure and permission-request path applies.

A busy originating conversation rejects edits and execution rather than queuing
a version selection. This includes queued or running descendants. Work owned by
another conversation does not block an idle origin. Version selection, state
publication and run admission share the existing session lock; observers cannot
reenter these operations.

## Proposal, checklist and permissions

| Concern | Meaning |
| --- | --- |
| Plan task | Investigation and proposal instructions; no additional authority restriction |
| ProposedPlan | One Markdown body with `plan_id` and `revision` |
| Todo | The existing implementation checklist; publishing a plan does not create or replace it |
| Permission preset | Read-only, workspace scope and network settings enforced during tool execution |
| Explicit read-only child | A permission ceiling selected for a delegated task, independent of Plan |

The Plan instructions discourage replacing the proposal with Todo items, but
`todo_write` is available whenever that plugin is enabled. Prompt instructions
express the intended task; they cannot guarantee that a model with write
permission will never issue a write.

The optional `git_read` helper supplies bounded local log/show/commit comparisons
without repository helpers or implicit network access. Ordinary Shell tools are
also available under the current permission preset. Independent Code Review uses
its own reviewer instructions even when launched from a Plan conversation.

## History and request boundaries

Plan state belongs to its originating main Runtime. Switching conversations does
not lend the visible conversation's proposal to an older runtime or child.
Saved history restores the proposal and workflow, with no active execution.
Historical execution records do not queue another run or restore authorization.

`plan` and `--plan` are startup inputs. An explicit startup
override applies to the selected initial conversation, including the resume
picker, before an initial prompt runs. It does not rewrite the user default for
later conversations. A retained origin with active work rejects such an override.

The plugin stores `work_mode`, `proposed_plan` and `plan_execution` records in the
existing SessionStore. A failed save blocks execution. Request-level instructions
are rebuilt for every model request and compaction context; saved proposals are
marked as proposals unless explicitly selected for the current run. This keeps
the task instructions present after tool calls, compaction and restoration.

Disable `plugins.plan.enabled` to remove its commands, proposal tool and request
instructions. Core operation does not require a Plan service or another task loop.

Implementation: [Plan plugin](../../../src/virgo/plugins/plan.py),
[Runtime](../../../src/virgo/harness/session.py),
[extension API](../development/extensions.md),
[existing end-to-end acceptance](../../../tests/harness/test_plan_workflow_e2e.py).
