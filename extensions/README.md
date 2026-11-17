# Virgo extensions

[Project home](../README.md) · [简体中文](README.zh-CN.md) · [Extension API](../docs/en/development/extensions.md)

Optional capabilities that live outside the installed package. Copy a file into
`~/.virgo/extensions/` (or `.virgo/extensions/` in a trusted project); Virgo loads
every `*.py` file there at session start.

| Extension | Install | Purpose |
| --- | --- | --- |
| [mcp.py](mcp.py) | Copy to `~/.virgo/extensions/mcp.py` and install the SDK extra | Connect configured stdio MCP servers and expose their tools |

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

The `mcpServers` setting is accepted as well. Server arguments and environment
values expand `${VARIABLE}`; disabled entries are skipped. Tools appear as
`mcp__<server>__<tool>` and remain subject to Core permissions: calls are treated
as unmanaged external operations. Host dispatches directly; Sandbox refuses
calls whose requested resource scope cannot be enforced. Grants cannot switch
to Host. Server startup and autonomous activity remain outside the managed boundary.

`/mcp` lists configured servers and tool counts. `/mcp reconnect` reloads the
configuration; a failed reload keeps the previous connections and tools.
`mcp_status` exposes the same local state to the model without contacting servers.
