# 004 — Untrusted project configuration isolation and approval binding

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/004-untrusted-project-config-isolation.md)

**Status:** Accepted

## Context

Opening an arbitrary repository should never allow checked-in project files to execute Host code, weaken sandbox modes, hijack user API endpoints, wipe user-level hooks/MCP servers via settings merges, or reuse a previous **Always allow** grant after an MCP server definition changes.

## Decision

- **Execution authority belongs exclusively to the user**: Project `.virgo/settings.json` may never set `sandbox`, `execution`, or `permissions` (`Settings.load()` in `virgo.harness.settings` raises immediately), nor can project settings or runtime overrides populate `extensions.trustedProjects` or install saved permission grants.
- **Pre-merge stripping of untrusted active config**: For any workspace not explicitly trusted via user-level `extensions.trustedProjects` or `--trust-project-extensions`, `Settings.load()` strips `hooks`, `mcpServers`, `providers`, `extensions`, `shell`, `search`, and `fetch` **before** `deep_merge` onto user settings, and extension/MCP loaders skip project `.virgo/extensions/*.py` and `.virgo/mcp.json`. Switching workspaces in the TUI resets `trust_project_extensions` to `False`.
- **Symlink rejection for project prompt files**: `virgo.harness.prompt` refuses to read symlinked project `AGENTS.md`, `CLAUDE.md`, `SYSTEM.md`, or `APPEND_SYSTEM.md` files so repository links cannot pull external files into model context.
- **Config-fingerprint-bound MCP approvals**: Read-only MCP tools run directly, while mutating MCP tools prompt in **Read-Only** and **Workspace-Edit**. **Always allow** persists under `f"{tool_name}:{config_fingerprint}"` using a SHA-256 fingerprint of expanded `command`/`args`/`env` (stdio) or canonical transport, full URL including query, and sorted lowercase expanded `headers` (HTTP/SSE). Any change invalidates the grant.
- **Same-turn denial honoring**: Explicit user declines (`n`) are enforced for the remainder of the user turn (`already_denied_this_turn`) across exact retries and broader/covered scopes, preventing repetitive prompt loops without blocking unrelated sibling paths or read access after a declined write.

## Tradeoffs and constraints

- **Explicit opt-in for project automation and routing**: Trust is required for these project settings and files, including `shell.executable`, `search` and `fetch` backends, and their `apiKeyEnv` settings. Ordinary project options such as `plugins.web.enabled` still follow the normal settings precedence.
- **No silent merge override**: Any future setting key capable of executing code, redirecting network endpoints, or disabling user protections must be added to `UNTRUSTED_PROJECT_KEYS` (or banned outright at project scope) and stripped before merging.
- **No automatic redirect replay on MCP HTTP calls**: URL-based MCP tool calls fail on HTTP redirects rather than re-submitting a potentially side-effecting request to another origin.

## Related

- [Extensions (Todos & MCP)](../guides/extensions.md)
- [Permissions & approvals](../guides/permissions.md)
- [Architecture overview](../development/architecture.md)
