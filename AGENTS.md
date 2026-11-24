# Development

- Install locked dependencies: `uv sync --locked --dev`.
- Lint changed Python files with `uv run --no-sync ruff check <paths>`.
- Run affected existing tests with `uv run --no-sync pytest -q <paths>`. Prefer assembled-session or real-process acceptance for execution changes.
- Native acceptance must run on real Linux, macOS, WSL2, and native Windows hosts with `VIRGO_REQUIRE_SANDBOX_TESTS=1`; see [execution-acceptance.md](docs/en/development/execution-acceptance.md). A skipped suite or a policy-generation check does not establish isolation.
- Preserve startup acknowledgement, cancellation, process-tree cleanup, and fail-closed behavior when changing execution. Never replay a possibly started payload automatically.
- When retiring tests during a breaking refactor, explicitly list every deleted test and its replacement coverage in the commit message and PR description.

# Documentation

- Documentation is organized hierarchically with symmetric English (`docs/en/`) and Simplified Chinese (`docs/zh-CN/`) trees:
  - Entry overview and navigation: `README.md`, `README.zh-CN.md`, and `docs/{en,zh-CN}/README.md`
  - User guides: `docs/{en,zh-CN}/guides/`
  - Architecture, internal interfaces, and acceptance: `docs/{en,zh-CN}/development/` ([architecture](docs/en/development/architecture.md), [execution interface](docs/en/development/execution-interface.md), [execution acceptance](docs/en/development/execution-acceptance.md))
  - Landed architectural decisions and constraints: `docs/{en,zh-CN}/decisions/` ([index](docs/en/decisions/README.md))
- Keep English and Chinese documentation consistent with implemented behavior. Update affected docs and decision records whenever code behavior, boundaries, or architecture change.
- Record only verified facts and landed decisions with lasting impact—never process notes, internal milestone IDs, or discarded proposals. Do not claim native guarantees from Broker-only fixtures.

# Execution Safety PR Review Criteria

Virgo's sandbox is a **guardrail, not a fortress**: it bounds occasional mistakes by a well-aligned model, protects private credentials and control metadata, and confirms high-risk or out-of-scope operations. Any PR touching execution, permissions, sandboxing, hooks, or MCP must answer these nine review questions:

1. **What mistake does this prevent?** Can you give a concrete example of an accidental error a benign model would realistically make? If a check only matters against deliberate evasion, it is out of scope (R1).
2. **Where does it sit on the risk hierarchy?** Prioritize (1) irreversible/remote mutations (force push, deploy, cloud/DB delete, `curl | sh`), (2) credential leaks, (3) control metadata & shell config rewrites (e.g. `.virgo`, `~/.zshrc`; workspace `.git` and `AGENTS.md` stay writable in Workspace-Edit), (4) writes outside the workspace, (5) reads outside the workspace (R2).
3. **Does it cause false positives or measurable slowdown?** Verify cold start (`<= 5s` across Tier 1 platforms: Linux, macOS, WSL2, and native Windows) and O(1) per-tool-call overhead against realistic repository sizes; excessive friction pushes users to No-Limit (R3).
4. **Does it add OS sandboxing around Virgo's own code or trusted user integrations?** Only Shell and model-spawned arbitrary commands belong in the OS sandbox; Broker file tools enforce `PermissionPolicy` directly, and extensions/hooks/MCP servers run on Host with trust-at-install/config-fingerprint approvals (R4).
5. **Can untrusted project configuration execute code or override/clear user settings?** Project hooks, `mcpServers`, `.virgo/mcp.json`, extensions, and `providers` require explicit user trust and must never clear or hijack user-level settings (R5).
6. **Is failure visible, actionable, and free of automatic replay?** Unavailable guardrails must fail closed with clear guidance or an explicit one-shot user choice, never silently fall back to Host or replay a possibly started command (R6).
7. **Is this the simplest mechanism?** Prefer fewer mechanisms and less code; document honest platform differences instead of chasing cross-platform parity with complex code (R7).
8. **Do the docs match the threat model?** State guardrail guarantees and non-goals honestly; never promise defense against malicious code, adversarial evasion, or prompt-injection attack chains (R8).
9. **Are security tradeoff changes prominently documented in the PR description?** Any widening or tightening of security boundaries must be called out visibly at the top of the PR description—never buried inside a refactor commit.
