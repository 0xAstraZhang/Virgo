# Virgo 扩展

[项目首页](../README.zh-CN.md) · [English](README.md)

位于已安装包之外的可选能力。把文件复制到 `~/.virgo/extensions/`（或受信任项目的
`.virgo/extensions/`）；Virgo 在会话启动时加载其中的每个 `*.py` 文件。
把 Python 扩展安装到用户目录，表示信任它使用当前账户的宿主权限。项目 Python 扩展、
命令钩子和 MCP 配置需要 `--trust-project-extensions`，或用户配置中的
`extensions.trustedProjects` 授权；项目配置不能自行授予信任。用户配置与启动参数中的
命令钩子同样属于受信任的宿主代码。原生沙箱不隔离这些 Python 扩展代码或宿主钩子。

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

也接受 `mcpServers` 配置项。项目未获信任时，会忽略项目中的服务器条目，包括项目配置中的
条目。服务器参数和显式环境值支持 `${VARIABLE}` 展开；禁用条目会被跳过。服务器不继承
宿主环境中的凭据；`env` 中的值属于受信任配置明确授予的能力，不能覆盖 `HOME`、临时目录、
Python 或动态加载器等启动控制变量。

本地服务器通过与 Shell、文件工具相同的 `SandboxExecutor` 启动。启动阶段、自主活动、
子进程、文件访问和网络访问均使用来源 Runtime 捕获的权限策略；服务器声明的只读提示
不能绕过操作系统边界。No-Limit 仍通过执行器运行，并使用宿主权限。服务器可执行文件及其
依赖必须位于策略可读取的范围；启动失败会报告服务器不可用，不回退到宿主执行。
Windows 应配置原生 `.exe` 的绝对路径，例如 `node.exe`，并把服务器脚本放在 `args` 中；
不能使用 `.cmd` 或 `.bat` 包装器。

stdio 传输仍须满足服务器运行时的要求。Linux 仅允许事件循环唤醒所需的匿名、已连接 Unix
stream socketpair；Unix socket 监听器及 datagram socketpair 仍被阻止。Windows 上 CPython
在 asyncio 导入 `_overlapped` 时就初始化 Winsock，之后还需创建 TCP 回环唤醒通道。
实测 Windows Server 2022/2025 的原生 Read-Only 在导入阶段拒绝访问；同一个 SDK 2.3.0
测试服务器在两台宿主的 Workspace-Edit 下均使用 `ProactorEventLoop` 完成了真实工具调用
和工作区写入。各模式均保持原有网络策略，Virgo 不会为启动事件循环扩大权限。
运行时无法初始化时，查看 `mcp_status` 和服务器
stderr；同步 stdio 成功不能证明 asyncio 兼容。详见 [Windows 运行时限制](../docs/zh-CN/guides/windows-sandbox.md)。

工具名为 `mcp__<server>__<tool>`。每个 Runtime 持有自己的持续连接，子 Agent 不借用
主 Agent 的服务器。捕获的权限策略改变时，旧连接会退役。现有 `request_permissions`
可为一次精确的本地 MCP 调用增加资源：该调用启动独立的临时服务器，并在返回结果前
清理其整个进程树；后续调用仍使用基础策略。临时服务器是新的 MCP 会话，不会自动复制
基础连接已经建立的会话状态。

服务器由连接自身的常驻线程启动，并由该线程监督整个连接生命周期，包括 Linux 的父线程
退出语义。传输错误会附带当前可用、长度受限并已脱敏的服务器 stderr 尾部。

`/mcp` 列出服务器、工具数量和连接状态；`/mcp reconnect` 显式重新加载配置，失败时保留
原有工具和连接。`mcp_status` 不联系服务器，返回同样的本地状态及实际执行边界。
取消调用会终止该连接的服务器进程树；此前可能已经产生副作用，Virgo 不自动重启或重放
该调用。连接失败后，可用 `/mcp reconnect` 显式建立新连接。

本扩展只支持本地 stdio 服务器。远程 MCP 不受本地沙箱保护，需要独立的外部能力与审批
适配器。MCP 的 Python 适配器本身仍属于受信任的宿主扩展。
