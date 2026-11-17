# Virgo 扩展

[项目首页](../README.zh-CN.md) · [English](README.md) · [扩展 API](../docs/zh-CN/development/extensions.md)

位于已安装包之外的可选能力。把文件复制到 `~/.virgo/extensions/`（或受信任项目的
`.virgo/extensions/`）；Virgo 在会话启动时加载其中的每个 `*.py` 文件。

| 扩展 | 安装 | 用途 |
| --- | --- | --- |
| [mcp.py](mcp.py) | 复制到 `~/.virgo/extensions/mcp.py`，并安装 SDK 附加依赖 | 连接已配置的 stdio MCP 服务器并暴露其工具 |

## MCP

MCP 由 `mcp.py` 扩展提供，使用官方
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)，
不是 Virgo 内置能力。

```bash
cp extensions/mcp.py ~/.virgo/extensions/mcp.py
uv tool install 'virgo[mcp]'    # 安装发行版
uv sync --extra mcp             # 源码检出
```

在 `~/.virgo/mcp.json` 或 `.virgo/mcp.json` 中按通用格式配置服务器：

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

也接受 `mcpServers` 配置项。服务器参数和环境值支持 `${VARIABLE}` 展开；禁用条目会被跳过。
工具名为 `mcp__<server>__<tool>`，仍受 Core 权限约束：调用被视为未受管远程操作，
离线或限定文件范围的任务需要显式的单次操作例外。

`/mcp` 列出已配置服务器和工具数量；`/mcp reconnect` 重新加载配置，失败时保留原有连接和工具。
`mcp_status` 向模型暴露同样的本地状态，不联系服务器。
