# Virgo extensions

[Project home](../README.md) · [简体中文](README.zh-CN.md)

Optional capabilities that live outside the installed package. Copy a file into
`~/.virgo/extensions/` (or `.virgo/extensions/` in a trusted project); Virgo loads
every `*.py` file there at session start. Installing a Python extension in the user
directory authorizes trusted code with the account's host rights. Project Python
extensions, command hooks, and MCP configuration require `--trust-project-extensions`
or a user-owned `extensions.trustedProjects` entry. Project settings cannot grant
that trust. User/startup command-hook configuration is also trusted host code.
The native sandbox does not contain Python extension code or host hooks.

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

Each call replaces the full list. `step`, `content` and `status` are required;
`explanation` applies to the entire update and is cleared when omitted. Steps
progress from `StepA` through `StepZ`, then `StepAA`, `StepAB`, and so on. A shared
step permits parallel work; the tool does not enforce scheduling. An empty list
clears the checklist. Multiple items may be `in_progress`.

Successful model-visible results contain only `Todos 状态更新成功`. Full state
stays in result details and follows the originating conversation and active
branch. If compaction removes the latest complete call/result pair while any
item remains unfinished, a request section retains the entire latest state,
including completed items and the explanation. It survives summary/native
compaction and resume without repeatedly appending snapshots to history. A
finished or cleared list is not pinned, and unfinished work does not force
another model turn. Old results without steps remain readable without inferred
grouping; subsequent writes require the new fields.

## MCP

MCP is provided by the `mcp.py` extension, which uses the official
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk).
It is not built into Virgo.

```bash
cp extensions/mcp.py ~/.virgo/extensions/mcp.py
uv tool install 'virgo[mcp]'    # installed distribution
uv sync --extra mcp             # source checkout
```

Configure servers in `~/.virgo/mcp.json` or `.virgo/mcp.json` using the common
format:

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

The `mcpServers` setting is accepted as well. Project server entries are ignored
until project trust is granted, including entries in project settings. Server
arguments and explicit environment values expand `${VARIABLE}`; disabled entries
are skipped. Ambient host credentials are not inherited. Explicit `env` values
are capability grants from trusted configuration; launcher variables such as
`HOME`, temporary directories, and Python/dynamic-loader controls cannot be
replaced.

Local servers launch through the same `SandboxExecutor` as Shell and filesystem
tools. Their startup, autonomous activity, subprocesses, filesystem access, and
network access use the originating runtime's captured policy. Read-only tool hints
do not bypass that OS boundary. No-Limit still uses the executor with host rights.
A server executable and its dependencies must be readable under the policy;
startup failure reports an unavailable server and never invokes a host fallback.
On Windows configure an absolute native `.exe` (for example `node.exe` with its
server script in `args`), rather than a `.cmd` or `.bat` shim.

Server runtime requirements apply even to stdio transports. Linux allows only
anonymous connected Unix stream socketpairs for event-loop wakeups; Unix socket
listeners and datagram socketpairs remain blocked. On Windows, CPython initializes
Winsock during asyncio's `_overlapped` import, before creating its later TCP
loopback wakeup pair. Native Read-Only denied this import on the tested Windows
Server 2022/2025 hosts. On both hosts, the same SDK 2.3.0 fixture completed a real
tool call and workspace write under Workspace-Edit using `ProactorEventLoop`.
Each mode retained its original network policy; Virgo does not expand permissions
to make an event loop start. Check `mcp_status` and
server stderr when a runtime cannot initialize; a synchronous stdio server working
does not establish asyncio compatibility. See [Windows runtime limits](../docs/en/guides/windows-sandbox.md).

Tools appear as `mcp__<server>__<tool>`. Each runtime owns its persistent connection;
a subagent cannot borrow the main agent's server. A changed captured policy retires
the old connection. The existing `request_permissions` flow can grant resources
for one exact local MCP call: that call uses a separate temporary server, and its
process tree is drained before the result returns. Subsequent calls use the base
policy. Temporary servers begin a new MCP session, so state established in a base
connection is not automatically copied into an elevated call.

The connection's owner thread launches and supervises its server for the full
connection lifetime, including Linux's parent-thread exit semantics. Transport
errors include the available bounded, redacted tail of server stderr.

`/mcp` lists servers, tool counts, and connection state. `/mcp reconnect` explicitly
reloads configuration; a failed reload keeps the previous tools and connections.
`mcp_status` reports the same local state and the actual executor boundary without
contacting servers. Cancellation terminates the connection's server tree; effects
may already exist, and Virgo does not automatically restart or replay the call.
Use `/mcp reconnect` to establish a replacement after a failed connection.

This extension supports local stdio servers only. Remote MCP transports remain
outside the local sandbox and require a separate external capability/approval
adapter. The MCP Python adapter itself remains a trusted host extension.
