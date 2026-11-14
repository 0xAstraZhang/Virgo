# Core and plugin boundaries

[Architecture](architecture.md) · [Extensions](extensions.md) · [Migration](../guides/migrating-to-core-plugins.md) · [简体中文](../../zh-CN/development/core-plugin-boundaries.md)

## Decision

Virgo is a coding harness. The Core owns reliable model–tool execution; optional capabilities use the same `setup(api)` extension interface. The official project maintains capabilities that directly support coding, rather than a general automation or long-lived agent organization platform.

This decision was implemented from `main@47e1757bda5c5cfafb3a5a508a1a6ec4d629bfaf`. The implementation uses the existing event bus, Runtime and `AgentSession`; it does not introduce another agent loop, dependency resolver, plugin marketplace, hot reloader or Python sandbox.

## Ownership

| Layer | Owns | Must not depend on |
| --- | --- | --- |
| AI/model adapters | Native request/response protocols, authentication, reasoning, provider-specific context and hosted capabilities | Capability plugin implementations |
| Agent/Tool runtime | Message ordering, streamed events, validation, tool batches, cancellation, bounded results | CLI, TUI or individual plugins |
| Harness Core | Session stores, runtime identity, context/compaction, mandatory permissions, diagnostic evidence, plugin loading and registration | Imported implementations of disabled plugins |
| Base coding tools | `read`, `write`, `edit`, `glob`, `grep`, and the selected shell | Task/team/workflow platforms |
| Official plugins | Focused optional tools, commands, prompt sections and event handlers | Private Session registries or the currently selected TUI pane |
| Hosts | CLI, headless output, Textual rendering and interaction | A mandatory `setup(api)` lifecycle |

Model adapters, hosts and capability plugins have different lifecycles. They need not share a plugin interface. Provider-native context is preserved; adapters do not flatten all models to a lowest-common-denominator protocol. Textual remains a host dependency in this change; packaging it separately is a later decision, not a completion metric.

## Official capabilities

| Plugin ID | Responsibility | `coding` default |
| --- | --- | --- |
| `skills` | Index and explicitly expand skill instructions | Enabled |
| `todo` | Checklist belonging to the current conversation | Enabled |
| `ask_user` | Questions, cancellation and unattended timeout handling | Enabled |
| `diagnostic_get` | Expose evidence already authorized to the caller | Enabled |
| `subagent` | Focused children with independent context and inherited restrictions | Enabled |
| `background` | Job tools, status, cancellation and owner-bound notifications | Enabled |
| `web` | Search/fetch routing, permission-aware access and configurable budgets | Enabled |
| `mcp` | Configured connections, tools and bounded recovery | Only when server configuration exists |
| `auto_review` | Optional model-assisted review for the Core permission policy | Enabled |

`minimal` disables the preset's optional capabilities. Explicit `plugins.<id>.enabled` values override the preset. Git remains available through the shell; there is no separate Git plugin.

The Core owns shell/process execution and cancellation. The background plugin uses those primitives rather than a second executor. The Core also owns diagnostics and the human-facing `/diagnostics` command; disabling `diagnostic_get` only removes the model-facing lookup tool.

## Runtime invariants

1. Core permission checks run before approval and immediately before execution, including after argument-changing hooks. A missing or failed policy cannot become unrestricted execution. Disabling model-assisted review uses the human approval path for uncertain operations.
2. Runtime identity, stores, diagnostic grants, cancellation and shared request budgets follow the caller. A child cannot raise the parent's permission ceiling. Switching the visible conversation does not transfer ownership.
3. Tool/command registration records an owner and rejects name collisions. Dynamic replacement validates the full candidate set before publication; new schemas become visible at request boundaries. An executing batch retains its tool snapshot.
4. MCP recovery validates a candidate before publication and retains a healthy connection when recovery fails. An active old response keeps its original schema and client until its batch completes; retired handles outside that response are rejected. Unused generations are reclaimed at request/turn/end boundaries. Recovery never replays a business operation. This also applies when the replacement advertises identical schemas: every reconnect publishes new handles at a request boundary.
5. Optional plugin setup failure is diagnosed, partial registration is rolled back, and degradation is visible. Cleanup callbacks execute in reverse registration order; a failed cleanup does not prevent later callbacks.
6. A disabled plugin is not imported, does not register tools or prompt sections, and does not start its own connections or background services. Runtime hot unload is not supported; configuration takes effect when constructing a new session.
7. Python extensions are trusted code in the process. Tool permission checks do not sandbox arbitrary Python. Project extensions require trust granted outside project-controlled settings. CLI trust applies only to the initial workspace; both saved-session switches and additional TUI panes reset it when opening another workspace.

## Retired subsystem dependency inventory

The inventory was completed before production changes. Removal deletes repository implementations and entry points, never user data.

| Subsystem | Retired dependencies | Historical data retained |
| --- | --- | --- |
| Schedule | Feature/commands, process-wide workspace ownership, polling thread, tool, TUI workspace and dedicated UI methods | `<project>/.virgo/schedules.json` |
| Workflow | Script loader, agent orchestration, journal/replay engine, command/tool and shipped example | `<user>/workflows/*.py`, `<project>/.virgo/workflows/*.py`, `<project>/.virgo/runs/*.jsonl` |
| Team | Long-lived workers, inbox/broadcast, plan approval, task-claim release and worktree helper | `<project>/.virgo/worktrees/`; `<session-directory>/<lead-id>-team/*.jsonl` |
| Tasks | Cross-session graph, ownership/dependencies, locks, tools and command | `<project>/.virgo/tasks/*.json` and lock files |
| Memory | Index/tool, model-assisted consolidation, shutdown extraction and prompt section | `<user>/memory/`, `<project>/.virgo/memory/` including `MEMORY.md` |
| Goal | Stop continuation/evaluator and command | `custom` entries with kind `goal` and auxiliary call records in session JSONL |

Existing message/tool-result records are data, not executable registrations. Old conversations remain readable when a tool has been removed. See the [migration guide](../guides/migrating-to-core-plugins.md) for inspection and manual export.

## Acceptance and measurement

The pre-implementation acceptance suite is `tests/harness/test_core_plugins_e2e.py`. It exercises a real assembled session with a deterministic model: the file/edit/shell loop and resume, execution-time permissions, registry publication, failed setup/cleanup, project trust, child restrictions and shared budget, historical data, and configuration precedence. Existing provider, MCP, background, cancellation, session and TUI scenarios remain the regression checks. [Test retirements](test-retirements.md) distinguishes removed product behavior from retained guarantees.

`ci/measure_startup.py` samples fresh-process import/construction time, module/thread/tool counts and schema/system character counts using a temporary home and the offline `faux` provider. Run with `--profile minimal` and `--profile coding` under the same interpreter and machine. Character counts are not tokens. The script does not measure coding quality, live-provider cost or success rate.

A coding-quality comparison needs fixed repository snapshots, model/effort, task set, permissions and budgets, then recorded completion, intervention, tool failures, model calls, elapsed time and cost. No quality gain is inferred from removed lines or the offline acceptance run. Keep measured results and their exact environment in the PR or a dated report.

The dated [validation report](core-plugins-validation.md) preserves the baseline suite result, five-process startup samples and the limits of the measured evidence.
