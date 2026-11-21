# Virgo extensions

[Project home](../README.md) · [简体中文](README.zh-CN.md)

Optional capabilities that live outside the installed package. Copy a file into
`~/.virgo/extensions/` (or `.virgo/extensions/` in a trusted project); Virgo loads
every `*.py` file there at session start.

| Extension | Install | Purpose |
| --- | --- | --- |
| [mcp.py](mcp.py) | Copy to `~/.virgo/extensions/mcp.py` and install the SDK extra | Connect configured stdio MCP servers and expose their tools |
| [todo.py](todo.py) | Copy to `~/.virgo/extensions/todo.py`; no extra dependencies | Model-owned checklist with ordered steps and compaction retention |

## Todos

```bash
mkdir -p ~/.virgo/extensions
cp extensions/todo.py ~/.virgo/extensions/todo.py
```

Repeat the copy when upgrading an older installation. The current extension updates
the conversation checklist directly in Read-Only and Workspace-Edit.
Restart Virgo after installing. A project-local copy requires project extension
trust; `--no-extensions` disables both locations. Todos is not an official plugin:
old `plugins.todo` and `features.todo` settings are ignored with a migration notice.

The main model receives `todo_write`, with no `/todos` or `/plan` command:

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

## MCP

MCP is provided by the `mcp.py` extension, which uses the official
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk).
It is not built into Virgo.

```bash
cp extensions/mcp.py ~/.virgo/extensions/mcp.py
uv tool install 'virgo[mcp]'    # installed distribution
uv sync --extra mcp             # source checkout
```

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

`/mcp` lists configured servers and tool counts. `/mcp reconnect` reloads
`mcp.json` and `settings.json`; a failed reload keeps the previous connections
and tools. `mcp_status` exposes the same local state to the model without
contacting servers.
