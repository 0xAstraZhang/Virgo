# Functional Requirements & Axiomatic Design Analysis

[Documentation](../README.md) · [Architecture overview](architecture.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/development/axiomatic-design.md)

This document captures Virgo's **Functional Requirements (FRs)**, **System Constraints (Cs)**, and **MIT Axiomatic Design** mapping ($\{FR\} = [A]\{DP\}$), alongside an explicit distinction between the **static import dependency matrix (DSM)** enforced in CI and the **functional-physical design matrix** $[A]$.

## Customer Need and System Constraints

In Axiomatic Design, global non-functional envelopes are **Constraints (Cs)** rather than Functional Requirements—every Design Parameter (DP) must respect them without claiming independence across constraints:

- **CA0 (Customer Need)**: Provide a lightweight, extensible, cross-platform terminal coding agent that helps explore and modify codebases, execute commands, and sustain long tasks safely with low daily friction.
- **C1 (Guardrail threat model)**: Bound occasional mistakes by a well-aligned model; adversarial evasion, malicious code containment, and prompt-injection attack chains are out of scope ([ADR 001](../decisions/001-guardrail-threat-model.md)).
- **C2 (Latency & scaling budgets)**: Cold start + first Shell command `<= 5s`, repeated short Shell command `<= 2s` with `O(1)` scaling, Broker `read` `<= 250ms` with `O(1)` scaling across Tier 1 platforms, and zero cold-start import cost for unconfigured optional SDKs ([Execution acceptance](execution-acceptance.md)).
- **C3 (Tier 1 platforms)**: Linux (`bubblewrap`), macOS (Seatbelt), unelevated native Windows 11 (Standard AppContainer), and Windows + WSL2 on Python 3.11+ ([ADR 003](../decisions/003-native-sandbox-backends.md)).
- **C4 (Fail-closed, no silent downgrade, no automatic replay)**: Unavailable native boundaries fail closed (with an explicit interactive one-shot **No-Limit** prompt leaving session mode unchanged), and failures after possible payload release never auto-replay ([ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)).
- **C5 (Failure-domain isolation)**: Broken plugins, extensions, diagnostics, or UI refresh hooks must never corrupt session history or crash core startup ([ADR 005](../decisions/005-runtime-layering-and-durable-state.md)).
- **C6 (Prompt-cache prefix stability)**: Ordinary turns stay append-only; system instructions and post-compaction retained sections stay frozen between compaction boundaries ([ADR 006](../decisions/006-three-layer-provider-agnostic-compaction.md)).

## Functional Requirements Decomposition (FR0 → Level 1 → Level 2)

- **FR0**: Collaboratively complete software engineering tasks in a user's workspace safely and continuously.
- **DP0**: Layered terminal coding-agent runtime (`src/virgo/`, Layers 0–5 coordinated by `AgentSession`).

| Level-1 FR | Level-2 Sub-FRs | Design Parameter (DP) |
| --- | --- | --- |
| **FR1: Authenticate and stream unified multi-provider LLM inference** | `FR1.1` Unified streaming adapters (`anthropic`, `openai`, `openai-responses`, `openai-codex`, `faux`)<br>`FR1.2` OAuth and API-key credential management (`/login`, `/logout`)<br>`FR1.3` Model catalog discovery/caching, reasoning effort mapping, and small-model routing (`/model`, `/effort`)<br>`FR1.4` Provider error classification (`retryable`, `overflow`, capability-unsupported, `fatal`) | **DP1**: `virgo.ai/*` (`providers/*`, `auth`, `oauth`, `catalog`, `models`) + `virgo.harness.model_registry` |
| **FR2: Enforce deny-first permissions, native OS sandboxing, and project trust** | `FR2.1` Three execution modes (`read-only`, `workspace-edit`, `no-limit`), unoverridable credential hard-denies, and sandbox env filtering<br>`FR2.2` Tier 1 native OS sandbox compilation (`bubblewrap`, Seatbelt, Windows Standard AppContainer)<br>`FR2.3` On-demand resource approvals, same-turn denial memory, and persistent project grants (`request_permissions`, `permission-grants.sqlite3`)<br>`FR2.4` High-risk Shell command one-shot confirmation and interactive one-shot No-Limit fallback<br>`FR2.5` Pre-merge stripping of untrusted project configs, symlink rejection on project prompts, and MCP config-fingerprint approval binding | **DP2**: `execution_policy`, `execution_types`, `sandbox/*`, `execution`, `harness/{settings,execution,resource_approvals,resource_rules,high_risk}` |
| **FR3: Execute workspace file operations, Shell commands, and bounded output storage** | `FR3.1` Broker-direct file tools (`read`, `write`, `edit`, `glob`, `grep`, `present_file`, `O_NOFOLLOW`, atomic replace, `.gitignore`)<br>`FR3.2` Shell interpreter discovery, binding, and process-tree supervision (`/shell`, `bash`/`powershell`, `HostProcess`, `WindowsProcess`)<br>`FR3.3` Bounded tool output spooling and paged/searchable inspection (`OutputStore`, `read_output`, 7-day TTL) | **DP3**: `file_access`, `process`, `output_store`, `tools/{fs,shell,bash,shell_env,output}`, `harness/shell_commands` |
| **FR4: Orchestrate multi-turn agent reasoning, tool dispatch, and run control** | `FR4.1` Stateless ReAct loop, schema validation, and bounded parallel vs sequential tool execution<br>`FR4.2` Live steering (`steer`), follow-ups, turn/call budgets, length continuation, and cancellation<br>`FR4.3` Exponential-backoff retries and fallback model switching | **DP4**: `virgo.agent/*` (`loop`, `agent`) + `virgo.harness.{runtime,runtime_limits}` |
| **FR5: Persist branched sessions, assemble prompts, and compact context windows** | `FR5.1` Append-only JSONL session tree with locking, incomplete-tail repair, and session switching/trash (`/new`, `/sessions`, `--continue`, `-r`, `session_info`)<br>`FR5.2` Immutable task origin & branch anchoring (`ExecutionOrigin`/`BranchSource`) and usage telemetry (`request_header`)<br>`FR5.3` Layered system prompt, project instructions, git/workspace snapshot, and prompt templates<br>`FR5.4` Three-layer provider-agnostic context compaction (`CompactionController` L0/L1/L2/L3, `/compact`) | **DP5**: `harness/{store,origin_context,prompt,compaction,compaction_records,telemetry}` |
| **FR6: Capture, isolate, persist, and surface runtime diagnostic evidence** | `FR6.1` No-I/O error capture, credential redaction, and emergency write fallback<br>`FR6.2` Isolated WAL SQLite diagnostic store and user query (`/diagnostics`)<br>`FR6.3` Runtime-scoped evidence projection, request-boundary & post-compaction delivery, and declared query tool (`diagnostic_get`) | **DP6**: `diagnostics`, `harness/{diagnostics,diagnostic_store,diagnostic_access,diagnostic_view,recovery}`, `plugins/diagnostic_get` |
| **FR7: Delegate isolated subagents and supervise background jobs** | `FR7.1` Reusable per-session subagents with isolated trace stores, turn limits, and effort overrides (`agent`, `SubagentStore`, `/subagents`)<br>`FR7.2` Core-owned background Shell and subagent job lifecycle, inspection, cancellation, and origin-routed wakeups (`JobManager`, `job_output`, `job_kill`, `/jobs`) | **DP7**: `harness/{jobs,subagents}`, `plugins/{subagent,background}` |
| **FR8: Integrate plugins, extensions, hooks, MCP, checklists, web access, and clarification** | `FR8.1` Transactional built-in plugins, overridable user/trusted-project Python extensions, and Shell command hooks (`EventBus`/`ExtensionAPI`, `ToolRegistry`)<br>`FR8.2` Skill discovery and invocation (`SKILL.md`, `/skills`, `/skill:name`)<br>`FR8.3` Lazy-loaded MCP stdio/HTTP/SSE client (`mcp__*`, `mcp_status`, `/mcp`)<br>`FR8.4` Model-owned checklist with epoch-frozen compaction retention (`todo_write`)<br>`FR8.5` Hosted vs client web search/fetch planning and fallback (`web_search`, `web_fetch`, `/search`, `/fetch`)<br>`FR8.6` Interactive model-to-user clarification with unattended timeout (`ask_user`) | **DP8**: `harness/{extensions,tool_registry}`, `plugins/{skills,web/*,ask_user}`, `extensions/{mcp,todo}`, `tools/{web,web_fetch}` |
| **FR9: Provide terminal presentation modes, command surface, and live localization** | `FR9.1` Fullscreen Textual TUI (streaming transcript, folding, approval/preference/session/subagent pickers, multi-pane workspace switch)<br>`FR9.2` Plain terminal REPL and non-interactive print/JSON mode (`-p --format json`)<br>`FR9.3` Explicit-capability `UI` interface, slash commands, and runtime bilingual switching (`en`/`zh-CN`, `/language`, `/status`, `/help`) | **DP9**: `virgo.cli/*` (`cli/tui/*`, `repl`, `console`, `main`), `harness/{ui,commands,preferences,auth_commands}`, `virgo.i18n/*` |

## Static Dependency Matrix (DSM) vs. Functional-Physical Design Matrix (`[A]`)

Virgo distinguishes two complementary views of system structure:

1. **Static Code Dependency Matrix (DSM) — Strictly Lower-Triangular**:
   Verified by `tests/test_architecture_matrix.py`:
   - **Zero package cycles** and **zero module cycles across all imports** (top-level, function-local, **and** `TYPE_CHECKING` imports), enabled by narrow Layer-3 protocols in `virgo.harness.contracts`.
   - **Strict unidirectional package layers** (Layers 0 → 1 → 2a → 2b → 3 → 4 → 5).
   - **Block-diagonal peer independence** across AI provider families, execution leaves (`file_access`, `output_store`, `sandbox`), built-in tool families (`fs`, `shell`, `web`, `web_fetch`), built-in plugins, and built-in extensions.
   - **Unidirectional service and presentation boundaries**: `ShellSelection` receives an optional `BackgroundLauncher` pushed from `JobManager` (`JobManager` resolves default shells only via injected callback); `CompactionController` and `Runtime` delegate request-extension fallback matching via `ExtensionAPI.add_request_fallback(..., matches=...)` without naming web error kinds; diagnostic query availability uses `ExtensionAPI.declare_diagnostic_query_tool(...)` rather than hardcoded tool names; and `cli/` interacts solely through `AgentSession`/`Runtime` façades.

2. **Functional-Physical Design Matrix `[A]` (`{FR} = [A]{DP}`) — Near-Lower-Triangular with Bounded Domain Loops**:

> Legend: **X** = diagonal DP implementing FR; `x` = decoupled lower-triangular dependence ($j < i$); **⊗** = bounded upper-triangular runtime feedback ($j > i$); `·` = independent.

| FR \ DP | DP1 | DP2 | DP3 | DP4 | DP5 | DP6 | DP7 | DP8 | DP9 |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **FR1** Model inference & auth | **X** | **⊗** | · | **⊗** | · | · | · | · | · |
| **FR2** Permissions, sandbox & trust | · | **X** | **⊗** | · | **⊗** | · | · | **⊗** | **⊗** |
| **FR3** Files, Shell & output store | · | `x` | **X** | · | · | · | · | · | · |
| **FR4** Agent reasoning & loop | `x` | `x` | `x` | **X** | **⊗** | **⊗** | · | **⊗** | · |
| **FR5** Sessions, prompt & compaction | `x` | `x` | `x` | `x` | **X** | **⊗** | · | **⊗** | · |
| **FR6** Diagnostics & recovery | `x` | `x` | `x` | `x` | `x` | **X** | · | · | · |
| **FR7** Subagents & background jobs | `x` | `x` | `x` | `x` | `x` | `x` | **X** | · | · |
| **FR8** Extensions, MCP, Todo, Web, Ask | `x` | `x` | `x` | `x` | `x` | · | `x` | **X** | **⊗** |
| **FR9** Presentation & localization | `x` | `x` | `x` | `x` | `x` | `x` | `x` | `x` | **X** |

### Intentionally Retained Domain Feedback Loops

Rather than forcing artificial indirection that would increase design complexity (Axiom 2), Virgo explicitly bounds and documents the remaining runtime feedback loops behind narrow contracts:

- **`FR4 (Runtime)` ↔ `FR5 (Compaction & SessionStore)`**: Cache-warm L2 in-context compaction intentionally reuses the active runtime's `request_context()` and model budget (C6), while persistence failures fail closed by aborting the active loop (C4/C5). Coupled via the narrow `CompactionHost` protocol and store failure handlers.
- **`FR2 (Approvals)` ↔ `FR9 (UI)` and `FR8 (ask_user)` ↔ `FR9 (UI)`**: Interactive approvals, one-shot No-Limit fallback, and `ask_user` inherently pause execution for human input. Bounded by explicit `UI` capability flags (`interactive`, `supports_confirm`, `supports_confirm_once`, `confirmation_handler()`).
- **`FR2 (Permissions)` ↔ `FR5 (Origin/Store)` and `FR2` ↔ `FR3 (File path resolution)`**: Permission grants and audits bind to `ExecutionOrigin`/`BranchSource` revisions, persistence failures block tool admission, and approval dialogs resolve canonical paths through `file_access`.
- **`{FR4, FR5}` ↔ `FR6 (Recovery diagnostics)`**: `RuntimeDiagnostics` delivers retained error summaries at natural request boundaries and post-compaction checkpoints without driving extra model turns.
- **`{FR2, FR4, FR5}` ↔ `FR8 (Hooks / Web / Todo)`**: Tool hooks run around the explicit permission boundary (`priority=100`) and disable hosted web offers when present; request preparers/fallbacks and request system sections participate in `request_context()` and compaction fallback through generic `ExtensionAPI` hooks.
- **`FR1 (Model metadata/settings)` ↔ `FR2 (Project trust)` & `FR4 (Busy state)`**: Untrusted project `providers` are stripped before settings merge, and background catalog metadata updates apply only when `AgentSession` is idle so in-flight request bindings remain immutable.

See also: [Architecture overview](architecture.md) · [Execution interface](execution-interface.md) · [ADR 005](../decisions/005-runtime-layering-and-durable-state.md)
