# 001 — Guardrail-not-fortress threat model

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/001-guardrail-threat-model.md)

**Status:** Accepted

## Context

Terminal coding agents face a tension between strong containment and low-friction daily development. Defending against malicious code, deliberate sandbox evasion, or prompt-injection attack chains requires heavy OS virtualization, aggressive filesystem restrictions, or high-friction prompts that slow cold start, break developer toolchains, and push users into running unrestricted.

## Decision

- Virgo's sandbox is explicitly designed as a **guardrail against occasional mistakes by a well-aligned model**, not a security boundary against malicious code, adversarial evasion, or prompt-injection attack chains (which require an external container or VM).
- Security work is prioritized by concrete accident risk (documented as R1–R8 in `AGENTS.md`):
  1. Irreversible or remote mutations (force pushes, protected-branch pushes, `git reset --hard`, `git clean -f`, `curl | sh`, recursive deletes outside the workspace, deploys/publishes, cloud deletes, destructive DB/cache CLI statements) require one-shot confirmation in sandboxed modes, and optionally in **No-Limit** when `permissions.confirmHighRisk: true` (`virgo.harness.high_risk`).
  2. High-sensitivity credential directories and files (`$VIRGO_HOME` plus 13 `PRIVATE_HOME_RELATIVES` in `virgo.execution_policy`) are hard-denied for reads and writes and cannot be granted via `request_permissions`, and credential/loader-injection environment variables are stripped from sandboxed Shell commands.
  3. Within **Workspace-Edit**, only project `.virgo` (`PROTECTED_NAMES = (".virgo",)`) triggers write confirmation, while `.git`, `AGENTS.md`, `CLAUDE.md`, and workspace `.venv` stay writable so routine Git commits, documentation edits, and package installs run without friction.
  4. Out-of-workspace writes and reads prompt on demand rather than silently succeeding or blocking all workflow progress.

## Tradeoffs and constraints

- **No anti-adversarial complexity**: Checks that only matter against deliberate evasion, TOCTOU races, malicious packages, or kernel bugs are out of scope if they add latency, maintenance burden, or false positives.
- **Latency budgets are first-class constraints**: Cold start and first Shell command must remain `<= 5s`, repeated Shell commands `<= 2s` with O(1) scaling, and Broker `read` `<= 250ms` with O(1) scaling across all Tier 1 platforms.
- **Honest documentation**: User and developer docs must state these non-goals explicitly and never claim container-grade or adversarial isolation.

## Related

- [Execution boundaries & sandbox](../guides/sandbox.md)
- [Permissions & approvals](../guides/permissions.md)
- [Native execution & performance acceptance](../development/execution-acceptance.md)
