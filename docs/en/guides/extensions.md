# Virgo extensions

[Documentation](../README.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/guides/extensions.md)

Virgo loads Python extensions through the shared `setup(api)` interface at
session start. Built-in extensions ship inside the package (`virgo.extensions`)
and require no separate installation; you can also add custom extensions—or
override a built-in extension by filename—in `~/.virgo/extensions/*.py` (or
`.virgo/extensions/*.py` in a trusted project).

| Built-in extension | Source | Purpose |
| --- | --- | --- |
| `mcp` | [src/virgo/extensions/mcp.py](../../../src/virgo/extensions/mcp.py) | Expose tools from stdio, Streamable HTTP, and legacy SSE MCP servers |
| `todo` | [src/virgo/extensions/todo.py](../../../src/virgo/extensions/todo.py) | Model-owned checklist (`todo_write`) with optional phases and compaction retention |

## Precedence and disabling

Extensions resolve by filename stem with precedence:

`built-in` < `~/.virgo/extensions/<name>.py` < trusted `.virgo/extensions/<name>.py`

- **Override a built-in extension**: place `todo.py` or `mcp.py` in
  `~/.virgo/extensions/` (or in a trusted project's `.virgo/extensions/`).
- **Disable specific extensions**: add their names to `extensions.disabled` in
  `~/.virgo/settings.json` (or trusted `.virgo/settings.json`):

```json
{
  "extensions": {
    "disabled": ["todo", "mcp"]
  }
}
```

- **Disable all extensions for a session**: pass `--no-extensions` (skips
  built-in, user, and project Python extensions; official plugins remain
  controlled by `plugins.<id>.enabled`).
- **Project trust**: project-level `.virgo/extensions/`, `.virgo/mcp.json`,
  `mcpServers`, and `extensions` settings are ignored until you trust the
  project via user-level `extensions.trustedProjects` or
  `--trust-project-extensions`.

## Todos (`todo`)

Enabled by default as a built-in extension. It works directly in **Read-Only**
and **Workspace-Edit**, and exposes `todo_write` to the main model (with no
`/todos` or `/plan` command):

```json
{
  "explanation": "Inspect both sides in parallel before implementing the change.",
  "todos": [
    {"step": "StepA", "content": "Inspect the API", "status": "in_progress"},
    {"step": "StepA", "content": "Inspect callers", "status": "in_progress"},
    {"step": "StepB", "content": "Implement the change", "status": "pending"}
  ]
}
```

Each call replaces the full list. `content` and `status` are required; `step` is
optional (for phased ordering: `StepA` through `StepZ`, then `StepAA`, `StepAB`,
and so on), and `explanation` applies to the entire update and is cleared when
omitted or `null`. A shared step permits parallel work; the tool does not enforce
scheduling. An empty list clears the checklist. Multiple items may be
`in_progress`.

Successful model-visible results contain only a short acknowledgement (`Todos
updated.` in English, `Todos 状态更新成功` in Chinese). Full state stays in result
details and follows the originating conversation and active branch. If compaction
removes the latest complete call/result pair while any item remains unfinished, a
request section retains the entire latest state, including completed items and
the explanation. It survives context compaction and resume without
repeatedly appending snapshots to history. A finished or cleared list is not
pinned, and unfinished work does not force another model turn. Old results
without steps remain readable without inferred grouping.

## MCP (`mcp`)

Enabled by default as a built-in extension using the official
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(included in Virgo's dependencies and imported lazily only when at least one
server is configured, so sessions without MCP servers incur zero SDK startup
cost).

Configure servers in `~/.virgo/mcp.json` (or `.virgo/mcp.json` in a trusted
project) using the common format:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
    },
    "issues": {
      "url": "https://mcp.example.org/mcp",
      "headers": {"Authorization": "Bearer ${ISSUES_TOKEN}"}
    },
    "legacy": {
      "type": "sse",
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

Choose one endpoint per server.

| Endpoint | Configuration |
| --- | --- |
| stdio | `command` launches a Host process via `SandboxExecutor`. `command`, optional `args`, and `env` support `${VARIABLE}` expansion. Native Windows supports PATH lookup and `.cmd` / `.bat` wrappers such as `npx`. |
| Streamable HTTP | `url` connects from the Host to an absolute HTTP or HTTPS endpoint. Omit the selector or set it to `http` or `streamable-http`. |
| Legacy HTTP+SSE | Set the selector to `sse` and `url` to the server's SSE endpoint. |

Use either `type` or `transport` as the selector. If both appear, they must select the same transport.
URL transports accept an optional `headers` object with string values. Names are case-insensitive and must
be unique. Virgo expands `${VARIABLE}` in header values and reports missing variables before connecting.
MCP errors and diagnostics redact full header values. For `Authorization` and `Proxy-Authorization`,
they also redact the credential following the scheme when it is at least three characters long.
Loopback URLs, including bracketed IPv6, are supported. Fragments and control characters are invalid.
Configure the final endpoint URL directly. Redirected tool calls fail and Virgo never resubmits the call.

Configuration precedence, from lowest to highest, is user `mcp.json`, user `settings.json`,
trusted project `.virgo/mcp.json`, then trusted project `.virgo/settings.json`.
The settings files use the same `mcpServers` key. Project entries require explicit trust via
`extensions.trustedProjects` or `--trust-project-extensions`.
Disabled entries are skipped. Each server has a bounded startup wait and one connection shared by
main and subagent runtimes. Use `callTimeout` to change the default 300-second tool-call timeout
and `includeTools` / `excludeTools` to filter tools by glob.

Tools appear as `mcp__<server>__<tool>`. Long or colliding names receive a deterministic hash suffix.
Read-only tools run directly. Mutating tools require approval in **Read-Only** and **Workspace-Edit** and run
directly in **No-Limit**. **Always allow** binds to a SHA-256 fingerprint. For stdio this covers the expanded
command, arguments, and environment overrides. For URLs it covers the canonical transport, the full
configured URL (including the query), and the expanded headers sorted by lowercase name.
Changing any of these inputs requires a new approval.

Each MCP text block has its own limit of 51,200 UTF-8 bytes and 2,000 lines,
including embedded resource text and JSON rendered as text. A block that exceeds
either limit displays a preview with an `output_id`. Use `read_output` in the
originating session to search (`pattern`), tail (`tail=true`), or page through the
full block by lines (`line_offset`/`line_limit`) or bytes (`offset`/`limit`).
Smaller blocks and images keep their original content and order.

`/mcp` is always available to list configured servers and tool counts, and
`/mcp reconnect` reloads `mcp.json` and `settings.json` (a failed reload keeps
the previous connections and tools). `mcp_status` is exposed to the model
dynamically whenever at least one server is configured (hidden otherwise to
save tool schema tokens) and reads local state without contacting servers.

See also: [Permissions & approvals](permissions.md) · [Architecture](../development/architecture.md) · [ADR 004](../decisions/004-untrusted-project-config-isolation.md)
