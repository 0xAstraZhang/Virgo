# Virgo 扩展

[项目首页](../README.zh-CN.md) · [English](README.md)

位于已安装包之外的可选能力。把文件复制到 `~/.virgo/extensions/`（或受信任项目的
`.virgo/extensions/`）；Virgo 在会话启动时加载其中的每个 `*.py` 文件。

| 扩展 | 安装 | 用途 |
| --- | --- | --- |
| [mcp.py](mcp.py) | 复制到 `~/.virgo/extensions/mcp.py`，并安装 SDK 附加依赖 | 连接已配置的 stdio MCP 服务器并暴露其工具 |
| [todo.py](todo.py) | 复制到 `~/.virgo/extensions/todo.py`，无需额外依赖 | 模型维护的清单，支持执行阶段和压缩保留 |

## Todos

```bash
mkdir -p ~/.virgo/extensions
cp extensions/todo.py ~/.virgo/extensions/todo.py
```

升级已安装的旧版时，再次运行复制命令。当前扩展可在 Read-Only 和 Workspace-Edit
中直接更新会话清单。安装后重启 Virgo。项目目录中的副本需要扩展信任；`--no-extensions` 禁用两个位置。
Todos 不再是内置插件，旧 `plugins.todo`、`features.todo` 配置被忽略并给出迁移提示。

主模型获得 `todo_write` 工具，无 `/todos` 或 `/plan` 命令：

```json
{
  "explanation": "并行检查接口与调用方，再进行修改。",
  "todos": [
    {"step": "StepA", "content": "检查接口", "status": "in_progress"},
    {"step": "StepA", "content": "检查调用方", "status": "in_progress"},
    {"step": "StepB", "content": "完成修改", "status": "pending"}
  ]
}
```

每次调用完整替换列表。每项必填 `step`、`content`、`status`；`explanation` 说明整个更新，
省略时清除旧说明。阶段按 `StepA` 到 `StepZ`，再到 `StepAA`、`StepAB` 等顺序推进。
同阶段可并行，工具不强制调度；允许多项同时处于 `in_progress`，空列表清空清单。

成功时只向模型返回“Todos 状态更新成功”。完整状态保存在结果 `details`，归属原始会话和
当前分支。若压缩移除了最新完整调用及成功结果，且仍有事项未完成，请求中会完整保留最新
状态，包括已完成项和说明。摘要压缩、原生压缩和恢复会话均保留状态，不向历史反复追加快照。
全部完成或清空后不再固定保留，未完成事项不会强制触发下一轮模型调用。旧结果缺少 Step 时
仍可读取，不推断分组；后续写入必须提供新字段。

## MCP

MCP 由 `mcp.py` 扩展提供，使用官方
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)，
不是 Virgo 内置能力。

```bash
cp extensions/mcp.py ~/.virgo/extensions/mcp.py
uv tool install 'virgo[mcp]'    # 安装发行版
uv sync --extra mcp             # 源码检出
```

在 `~/.virgo/mcp.json`（或受信任项目的 `.virgo/mcp.json`）中按通用格式配置服务器：

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

也接受 `mcpServers` 配置项（项目级的 `mcpServers` 与 `.virgo/mcp.json` 需通过
`extensions.trustedProjects` 或 `--trust-project-extensions` 显式信任）。服务器参数和环境值支持
`${VARIABLE}` 展开；禁用条目会被跳过。MCP 服务器通过 `SandboxExecutor` 在 Host 启动，主会话与所有子
Agent 共享同一个连接（原生 Windows 支持通过 `PATH` 查找命令与 `.cmd`/`.bat`，如 `npx`）。工具名为
`mcp__<server>__<tool>`：只读工具直接执行，可能修改宿主状态的工具在 Read-Only 与 Workspace-Edit
下需经审批（“始终允许”绑定服务器展开后的 `command`、`args`、`env` 的 SHA-256 指纹），在 No-Limit 下直接执行。

`/mcp` 列出已配置服务器和工具数量；`/mcp reconnect` 重新加载配置，失败时保留原有连接和工具。
`mcp_status` 向模型暴露同样的本地状态，不联系服务器。
