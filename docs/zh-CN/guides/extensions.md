# Virgo 扩展

[项目首页](../../../README.zh-CN.md) · [English](../../en/guides/extensions.md)

Virgo 在会话启动时通过统一的 `setup(api)` 接口加载 Python 扩展。内置扩展随包分发（位于
`virgo.extensions`），无需单独安装或复制；你也可以在 `~/.virgo/extensions/*.py`（或受信任项目的
`.virgo/extensions/*.py`）中新增自定义扩展，或使用同名文件覆盖内置扩展。

| 内置扩展 | 源码 | 用途 |
| --- | --- | --- |
| `mcp` | [src/virgo/extensions/mcp.py](../../../src/virgo/extensions/mcp.py) | 在 Host 上连接已配置的 stdio MCP 服务器并暴露其工具 |
| `todo` | [src/virgo/extensions/todo.py](../../../src/virgo/extensions/todo.py) | 模型维护的清单（`todo_write`），支持可选执行阶段与压缩保留 |

## 优先级与禁用方式

扩展按文件主名（stem）解析，优先级为：

`内置扩展` < `~/.virgo/extensions/<name>.py` < 受信任 `.virgo/extensions/<name>.py`

- **覆盖内置扩展**：将自定义 `todo.py` 或 `mcp.py` 放到 `~/.virgo/extensions/`（或受信任项目的
  `.virgo/extensions/`）即可覆盖对应的内置扩展。
- **按名称禁用扩展**：在 `~/.virgo/settings.json`（或受信任 `.virgo/settings.json`）中配置
  `extensions.disabled`：

```json
{
  "extensions": {
    "disabled": ["todo", "mcp"]
  }
}
```

- **单次会话禁用全部扩展**：启动时传入 `--no-extensions`（同时跳过内置、用户及项目 Python 扩展；
  官方插件仍由 `plugins.<id>.enabled` 控制）。
- **项目信任边界**：项目级 `.virgo/extensions/`、`.virgo/mcp.json`、`mcpServers` 与
  `extensions` 配置在显式信任项目前（用户级 `extensions.trustedProjects` 或
  `--trust-project-extensions`）都会被忽略。

## Todos（`todo`）

作为内置扩展默认启用，可在 Read-Only 和 Workspace-Edit 中直接更新会话清单。主模型获得
`todo_write` 工具，无 `/todos` 或 `/plan` 命令：

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

每次调用完整替换列表。每项必填 `content`、`status`；`step` 为可选阶段标签（按
`StepA` 到 `StepZ`，再到 `StepAA`、`StepAB` 等顺序推进），`explanation` 说明整个更新，
省略或为 `null` 时清除旧说明。同阶段可并行，工具不强制调度；允许多项同时处于
`in_progress`，空列表清空清单。

成功时只向模型返回简短确认（英文模式为“Todos updated.”，中文模式为“Todos 状态更新成功”）。
完整状态保存在结果 `details`，归属原始会话和当前分支。若压缩移除了最新完整调用及成功结果，
且仍有事项未完成，请求中会完整保留最新状态，包括已完成项和说明。摘要压缩、原生压缩和恢复
会话均保留状态，不向历史反复追加快照。全部完成或清空后不再固定保留，未完成事项不会强制触发
下一轮模型调用。旧结果缺少 Step 时仍可正常读取，不推断分组。

## MCP（`mcp`）

作为内置扩展默认启用，使用官方
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)
（已包含在 Virgo 核心依赖中，仅在配置了至少一个服务器时延迟导入，未配置 MCP 的会话零 SDK 冷启动开销）。

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

也接受 `mcpServers` 配置项（优先级：`~/.virgo/mcp.json` < `~/.virgo/settings.json` <
受信任 `.virgo/mcp.json` < 受信任 `.virgo/settings.json`；项目级的 `mcpServers` 与
`.virgo/mcp.json` 需通过 `extensions.trustedProjects` 或 `--trust-project-extensions`
显式信任）。服务器参数和环境值支持 `${VARIABLE}` 展开；禁用条目会被跳过。每个服务器支持
可选的 `callTimeout`（覆盖默认 300s 单次调用超时）及 `includeTools` / `excludeTools`
（按 glob 模式过滤暴露的工具）。MCP 服务器通过 `SandboxExecutor` 在 Host 启动，启动期
采用有界等待以避免阻塞冷启动，主会话与所有子 Agent 共享同一个连接（原生 Windows 支持通过
`PATH` 查找命令与 `.cmd`/`.bat`，如 `npx`）。工具名为 `mcp__<server>__<tool>`（超长或
冲突名称会自动追加确定性哈希后缀）：只读工具直接执行，可能修改宿主状态的工具在 Read-Only
与 Workspace-Edit 下需经审批（“始终允许”绑定服务器展开后的 `command`、`args`、`env`
的 SHA-256 指纹），在 No-Limit 下直接执行。

每个 MCP 文本块的上限为 51,200 个 UTF-8 字节和 2,000 行，嵌入资源中的文本及渲染为文本的
JSON 也按块计量。超过任一上限时，该块会显示预览和一个 `output_id`。在发起调用的会话中，
可用 `read_output` 按字节指定 `offset` 和 `limit`，分页读取该块的完整内容。限额内的文本块
和图像保留原始内容及顺序。

`/mcp` 命令始终可用，用于列出已配置服务器与工具数量；`/mcp reconnect` 重新加载 `mcp.json`
与 `settings.json`，失败时保留原有连接和工具。`mcp_status` 工具仅在配置了服务器时动态向模型
暴露（未配置时隐藏以节省工具上下文），读取本地状态而不联系服务器。
