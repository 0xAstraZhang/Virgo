# 工具与插件

[文档首页](../README.md) · [English](../../en/reference/tools.md)

模型只会获得当前角色、配置和能力允许的工具。扩展、MCP 连接和网页路由可能改变实际列表。工具名、参数名和枚举值不随界面语言变化。

Core 负责文件、所选 Shell 和权限申请工具。下列可选工具由 `web`、`ask_user`、`subagent`、`background`、`diagnostic_get` 提供，默认启用；`plugins.<id>.enabled` 可逐个覆盖。[Todos 扩展](../../../extensions/README.zh-CN.md#todos)提供 `todo_write`。[MCP 扩展](../../../extensions/README.zh-CN.md)另外提供 `mcp_status` 和每个服务器对应的工具。详见[配置](configuration.md#插件)。

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

## 权限申请

`request_permissions(tool, arguments, permissions, reason)` 申请单次操作例外。
三个可选权限字段为 `read_only`、`workspace_only`、`network_allowed`。批准不执行或重放
操作；同一来源运行中下一次相同工具调用消费授权。目标限于受管文件、Shell、Web 和 MCP
操作；子任务继续继承来源权限，可在内部为具体操作申请例外。显式 deny 和显式只读子任务上限始终有效，
权限预设保持不变。模型可见内容及 TUI 共享结构化拒绝信息：失败维度、目标、命令启动与副作用
状态。后端不可用与权限拒绝分开报告。

## 清单与提问

`todo_write` 仅在安装 [Todos 扩展](../../../extensions/README.zh-CN.md#todos)后提供。调用时以完整 `todos` 列表替换当前清单，每项必填 `step`、`content`、`status`；状态为 `pending`、`in_progress` 或 `completed`。`StepA`、`StepB`……`StepZ`、`StepAA`……表示执行顺序，同一 Step 的事项可并行，允许多个事项同时处于 `in_progress`。顺序由模型遵循，工具不强制调度。顶层可选 `explanation` 说明整个更新，省略时不沿用上次说明。空列表清空清单，非法输入保留旧状态。

成功调用只向模型返回“Todos 状态更新成功”，完整状态保存在会话结果的 `details` 中。若压缩移除了最新完整调用及成功结果，且仍有未完成事项，请求中会完整保留最新列表与说明，包括已完成项；适用于摘要压缩、原生压缩、再次压缩和恢复会话。全部完成或清空后不再固定保留。无 `/todos` 或 `/plan` 命令，未完成事项本身不会强制触发下一轮模型调用。

`ask_user` 允许主 Agent 提问，提供最多三个选项加自定义输入，或仅使用自由输入。回答澄清问题不确认执行。非交互、取消和超时结果会报告 `answered=false`，不会编造答案。无人值守运行使用 `askUser.unattendedTimeout`；用户加入对话后可以撤销该超时。

## 聚焦的子 Agent

`agent(description, prompt, read_only=false, background=false)` 使用独立上下文处理任务并返回最终答案，所需上下文必须包含在任务提示里。`read_only=true` 保持正常工具 schema，在执行时施加文件写入上限，包括 Shell。只读能力与操作系统沙箱分别设置。默认子代理不施加额外限制，但不能突破父代理权限上限。子 Agent 不递归创建子 Agent，默认最多 50 轮；即使最终失败，之前的工具调用仍可能有副作用。父子共享取消信号与 `budget.maxModelCalls` 模型调用预算。达到轮数上限、最终回答中断或最终回答为空时，会明确报告**未完成**，而不是成功。子代理失败时，父代理能收到有长度限制的近期工具错误原因及明确传递的诊断证据；成功时仍只返回最终回答。旧的 `type` 参数不再受支持，传入时会拒绝调用。参见[迁移指南](../guides/migrating-to-core-plugins.md#子代理能力迁移)。

## 后台任务

后台命令和子 Agent 共用任务管理器。`/jobs` 查看列表，`job_output` 读取输出，`job_kill` 取消任务。完成通知绑定原始对话，可插入正在运行的 Agent 或唤醒空闲 Agent，不会转移到界面当前选中的其他对话。

后台子 Agent 的完成通知包含完整的最终回答。后台命令的通知只包含已捕获输出的最后 1,200 个字符；可使用 `job_output` 读取更多内容。后台可调用任务若返回错误但没有结构化诊断，其任务记录会保留经过脱敏和长度限制的原始失败文本，而不是仅记录笼统状态。

客户端 `web_search` 和 `web_fetch` 的传输错误会标明异常类型（如 `ConnectError` 或 `ReadTimeout`），但不会显示可能包含私密 URL 或凭据的原始异常文本。

## MCP 扩展

MCP 是扩展而非内置插件。将 [`extensions/mcp.py`](../../../extensions/README.zh-CN.md) 复制到用户扩展目录或受信任项目的扩展目录，安装可选 `mcp` SDK 附加依赖，再在用户或项目的 `mcp.json` 或 `mcpServers` 配置项中配置 stdio 服务器。参数和环境字段支持 `${VARIABLE}` 插值。工具名为 `mcp__<server>__<tool>`，禁用的条目不会启动。服务描述保留第三方原文。

未安装扩展时，不启动 MCP 连接，也不注册 `mcp_status`。安装后，模型的 `mcp_status` 读取本地连接状态和工具数量，不联系服务器。

MCP 调用遵守联网开关；服务无法约束本地文件范围时，需要明确的单次操作例外。服务启动及自主活动在任务沙箱之外运行，离线预设不代表隔离了该服务进程。

`/mcp` 显示连接状态，`/mcp reconnect` 重新加载服务器配置；替换失败时保留原有连接和工具。不提供模型侧的重连工具。具体运行时边界见[故障排查](../guides/troubleshooting.md)。

实现：[基础工具](../../../src/virgo/tools)、[官方插件](../../../src/virgo/plugins)、[MCP 扩展](../../../extensions/mcp.py)、[扩展 API](../../../src/virgo/harness/extensions.py)。

## 已移除的产品功能

Schedule、Workflow、Team、持久 Tasks、通用 Memory 和 Goal 不再注册工具或命令，原有文件和历史工具结果保留。通过[迁移指南](../guides/migrating-to-core-plugins.md)检查旧数据，再按需要迁为项目脚本、CI 或外部系统。
