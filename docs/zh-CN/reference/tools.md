# 工具与插件

[文档首页](../README.md) · [English](../../en/reference/tools.md)

模型只会获得当前角色、配置和能力允许的工具。扩展、MCP 连接和网页路由可能改变实际列表。工具名、参数名和枚举值不随界面语言变化。

Core 始终提供文件和所选 Shell 工具。下列可选工具由 `web`、`todo`、`ask_user`、`subagent`、`background`、`mcp`、`diagnostic_get` 提供。可使用默认 `coding` 预设，或通过 `plugins.<id>.enabled` 选择能力；`--profile minimal --no-extensions` 仅暴露 Core 工具。详见[配置](configuration.md#预设与插件)。

## 文件、命令与网页

| 工具 | 用途 |
| --- | --- |
| `read` | 读取文本或受支持的图片；大文件通过 `offset`、`limit` 分页 |
| `write` | 创建或替换文件 |
| `edit` | 替换文件中精确匹配的文本 |
| `glob` | 按模式查找路径 |
| `grep` | 搜索文件内容 |
| `bash`／`powershell` | 使用选定的解释器执行命令 |
| `web_search` | 通过已暴露的客户端后端搜索 |
| `web_fetch` | 通过已暴露的客户端后端读取网页 |

读取和工具输出均有预算限制，长结果提供继续读取方式或已保存的输出路径。同一文件的写操作串行执行。原生网页服务可能提供服务端工具，而非上述客户端工具名。详见[命令解释器](../guides/shells.md)和[网页能力](../guides/web.md)。

## 清单与提问

`todo_write` 使用传入的完整列表替换会话清单。状态为 `pending`、`in_progress` 和 `completed`，允许多个事项同时进行；输入不合法时保留旧列表。`/todos` 显示清单。未完成事项本身不会强制触发下一轮模型调用。

`ask_user` 允许主 Agent 提问，提供最多三个选项加自定义输入，或仅使用自由输入。非交互、取消和超时结果会报告 `answered=false`，不会编造答案。无人值守运行使用 `askUser.unattendedTimeout`；用户加入对话后可以撤销该超时。

## 聚焦的子 Agent

`agent` 工具使用独立上下文处理明确任务，并返回最终答案。内置类型为 `general` 和只读 `explore`，自定义类型放在 `agents/*.md`。子 Agent 不递归创建子 Agent，默认最多 50 轮。即使最终响应失败，之前的工具调用也可能已经产生效果。子任务继承调用方的权限上限和取消信号；`budget.maxModelCalls` 可限制父子运行时共享的模型请求数。自定义类型和角色不能提升权限。

## 后台任务

后台命令和子 Agent 共用任务管理器。`/jobs` 查看列表，`job_output` 读取输出，`job_kill` 取消任务。完成通知绑定原始对话，可插入正在运行的 Agent 或唤醒空闲 Agent，不会转移到界面当前选中的其他对话。

## MCP 与诊断

默认预设下，没有服务器配置就不启动 MCP 连接，也不注册 MCP 管理工具；显式启用后可在新增服务器前使用管理入口。

stdio MCP 服务器配置在用户或项目的 `mcp.json`，或 `mcpServers` 配置项中。参数和环境字段支持 `${VARIABLE}` 插值。工具名为 `mcp__<server>__<tool>`，禁用的条目不会启动。除非作者提供本地化，服务和扩展的描述保留第三方原文。

`/mcp` 显示连接状态，`/mcp reconnect` 重新加载服务器配置。模型的 `mcp_status` 读取本地状态，`mcp_reconnect` 针对特定服务和已知错误执行有界、受权限约束的恢复。重连不会重放失败的业务操作。`diagnostic_get` 只暴露已授予调用 Runtime 的诊断证据，具体边界见[故障排查](../guides/troubleshooting.md)。

实现：[基础工具](../../../src/virgo/tools)、[官方插件](../../../src/virgo/plugins)、[扩展 API](../../../src/virgo/harness/extensions.py)。

## 已移除的产品功能

Schedule、Workflow、Team、持久 Tasks、通用 Memory 和 Goal 不再注册工具或命令，原有文件和历史工具结果保留。通过[迁移指南](../guides/migrating-to-core-plugins.md)检查旧数据，再按需要迁为项目脚本、CI 或外部系统。
