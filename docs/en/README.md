# Virgo Documentation

[Project home](../../README.md) · [简体中文](../zh-CN/README.md)

Documentation is organized by audience and depth so you can read only what you need.

## Guides

User-facing behavior, platform setup, and built-in extensions:

- [Permissions & approvals](guides/permissions.md) — Execution modes (`Read-Only`, `Workspace-Edit`, `No-Limit`), on-demand approvals, session/saved grants, same-turn denials, and high-risk command confirmation.
- [Execution boundaries & sandbox](guides/sandbox.md) — Guardrail threat model, credential and `.virgo` protection, temporary directory lifetimes, platform backend overview, and Broker/Host tool boundaries.
- [Native Windows sandbox](guides/windows-sandbox.md) — Standard AppContainer backend on updated Windows 11, requirements, unsupported-build fallback, and platform caveats.
- [Windows + WSL2 setup](guides/wsl2.md) — Running Virgo with the Linux `bubblewrap` backend inside WSL2.
- [Extensions (Todos & MCP)](guides/extensions.md) — Built-in `todo` and `mcp` extensions, precedence, project trust, transports, redaction, and output bounds.

## Development & architecture

System layout, internal contracts, and verification:

- [Architecture overview](development/architecture.md) — Package dependency layers, Core tools vs built-in plugins vs built-in extensions, and durable local state stores.
- [Functional requirements & Axiomatic Design](development/axiomatic-design.md) — Overall goal, functional domains, constraints, static DSM scope, and sparse leaf FR–DP impact analysis.
- [Leaf functional requirements](development/functional-requirements.md) — Observable outcomes, adjustable design mechanisms, and acceptance boundaries.
- [Functional acceptance](development/functional-acceptance.md) — Requirement-to-test mapping, controlled functional effects, and limits of the evidence.
- [Execution interface](development/execution-interface.md) — Core `PermissionPolicy`, `SandboxExecutor`, `ProcessRequest`, native backend compilation, Broker file tools, `OutputStore`, and Host MCP contracts.
- [Native execution & performance acceptance](development/execution-acceptance.md) — Cross-platform native isolation suites, CI gates, and realistic-scale latency budgets.

## Architectural decisions

The [decision records index](decisions/README.md) lists adopted decisions, their tradeoffs, and the constraints they establish.

## Examples

- [Project customization examples](../../examples/README.md)
