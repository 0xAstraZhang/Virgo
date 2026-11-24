# Virgo extensions

[Project home](../../../README.md) · [简体中文](../../zh-CN/guides/extensions.md)

Virgo loads Python extensions through the shared `setup(api)` interface at
session start. Built-in extensions ship inside the package (`virgo.extensions`)
and require no separate installation; you can also add custom extensions—or
override a built-in extension by filename—in `~/.virgo/extensions/*.py` (or
`.virgo/extensions/*.py` in a trusted project).

| Built-in extension | Source | Purpose |
| --- | --- | --- |
| `mcp` | [src/virgo/extensions/mcp.py](../../../src/virgo/extensions/mcp.py) | Connect configured stdio MCP servers on Host and expose their tools |
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

Enabled by default as a built-in extension. It works directly in Read-Only and
Workspace-Edit, and exposes `todo_write` to the main model (with no `/todos` or
`/plan` command):

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
the explanation. It survives summary/native compaction and resume without
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
    }
  }
}
```

The `mcpServers` setting is accepted as well (precedence: `~/.virgo/mcp.json` <
`~/.virgo/settings.json` < trusted `.virgo/mcp.json` < trusted
`.virgo/settings.json`; project-level `mcpServers` and `.virgo/mcp.json` require
explicit project trust via `extensions.trustedProjects` or
`--trust-project-extensions`). Server arguments and environment values expand
`${VARIABLE}`; disabled entries are skipped. Optional per-server `callTimeout`
overrides the default 300s tool-call timeout, and `includeTools` / `excludeTools`
filter exposed MCP tools with glob patterns. Servers launch on Host via
`SandboxExecutor` with bounded startup wait and one shared connection per server
across main and subagent runtimes (including `PATH` lookup and `.cmd`/`.bat`
wrappers such as `npx` on native Windows). Tools appear as
`mcp__<server>__<tool>` (long or colliding names receive a deterministic hash
suffix): read-only tools run directly, while mutating tools require approval in
Read-Only and Workspace-Edit (with **Always allow** bound to the SHA-256
fingerprint of the server's expanded `command`, `args`, and `env`) and run
directly in No-Limit.

Each MCP text block has its own limit of 51,200 UTF-8 bytes and 2,000 lines,
including embedded resource text and JSON rendered as text. A block that exceeds
either limit displays a preview with an `output_id`. Use `read_output` in the
originating session to page through the full block with byte `offset` and `limit`.
Smaller blocks and images keep their original content and order.

`/mcp` is always available to list configured servers and tool counts, and
`/mcp reconnect` reloads `mcp.json` and `settings.json` (a failed reload keeps
the previous connections and tools). `mcp_status` is exposed to the model
dynamically whenever at least one server is configured and hidden otherwise to
save tool schema tokens.
