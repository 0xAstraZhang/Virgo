# 工具与插件

[文档首页](../README.md) · [English](../../en/reference/tools.md)

模型只会获得当前角色、配置和能力允许的工具。扩展、MCP 连接和网页路由可能改变实际列表。工具名、参数名和枚举值不随界面语言变化。

Core 负责文件、所选 Shell 和权限申请工具。可选 Plan 插件提供已提交 Git 内容读取和 ProposedPlan。下列可选工具由 `web`、`todo`、`ask_user`、`subagent`、`background`、`mcp`、`diagnostic_get` 提供。可使用默认 `coding` 预设，或通过 `plugins.<id>.enabled` 选择能力；`--profile minimal --no-extensions` 提供完整 Core 工具 schema，不按任务过滤。详见[配置](configuration.md#预设与插件)。

## 文件、命令与网页

| 工具 | 用途 |
| --- | --- |
| `read` | 读取文本或受支持的图片；大文件通过 `offset`、`limit` 分页 |
| `write` | 创建或替换文件 |
| `edit` | 替换文件中精确匹配的文本 |
| `glob` | 按模式查找路径 |
| `grep` | 搜索文件内容 |
| `git_read` | 读取本地已提交日志、补丁或两个提交之间的差异，不执行仓库 helper |
| `bash`／`powershell` | 使用选定的解释器执行命令 |
| `web_search` | 通过已暴露的客户端后端搜索 |
| `web_fetch` | 通过已暴露的客户端后端读取网页 |

读取和工具输出均有预算限制，长结果提供继续读取方式或已保存的输出路径。同一文件的写操作串行执行。原生网页服务可能提供服务端工具，而非上述客户端工具名。详见[命令解释器](../guides/shells.md)和[网页能力](../guides/web.md)。

`edit` 保留替换范围以外的换行，包括 LF/CRLF 混合文件；插入的换行采用所在位置的换行形式。目录 `grep` 跳过不可读或禁止读取的文件条目，不读取其内容；显式指定禁止访问的路径仍会失败。图片超过 5 MiB 时会被拒绝，读取时限制载荷大小。

## 权限申请

`request_permissions(tool, arguments, permissions, reason)` 申请单次操作例外。
三个可选权限字段为 `read_only`、`workspace_only`、`network_allowed`。批准不执行或重放
操作；同一来源运行中下一次相同工具调用消费授权。目标限于受管文件、Shell、Web 和 MCP
操作；子任务继续继承来源权限，可在内部为具体操作申请例外。显式 deny 和显式只读子任务上限始终有效，
权限预设保持不变。模型可见内容及 TUI 共享结构化拒绝信息：失败维度、目标、命令启动与副作用
状态。后端不可用与权限拒绝分开报告。

## 清单与提问

`propose_plan(markdown)` 发布或替换完整 Markdown ProposedPlan。它属于可选 Plan 插件，对主 Runtime 可用，不要求当前处于 Plan；保存带版本的对话元数据，不写项目文件，也不授权执行。修订会使旧版本的执行选择失效。`/plan` 显示方案，`/plan execute <plan-id>:<revision>` 是用户明确开始执行的操作。编辑、恢复和权限边界见[先规划，再执行](../guides/planning.md)。

`todo_write` 使用传入的完整列表替换现有执行清单。状态为 `pending`、`in_progress` 和 `completed`，允许多个事项同时进行；输入不合法时保留旧列表。`/todos` 显示清单，未完成事项本身不会强制触发下一轮模型调用。Plan 中仍提供该工具：ProposedPlan 说明方案，Todo 在 Default 中跟踪执行进度。确认计划不会自动创建 Todo，也不引入第二套任务存储。

`ask_user` 允许主 Agent 提问，提供最多三个选项加自定义输入，或仅使用自由输入。Plan 指令要求先调查仓库，只对仍无法解决、且实质影响方案的决策使用该工具；回答澄清问题不确认执行。非交互、取消和超时结果会报告 `answered=false`，不会编造答案。无人值守运行使用 `askUser.unattendedTimeout`；用户加入对话后可以撤销该超时。

## 聚焦的子 Agent

`agent(description, prompt, read_only=false, background=false)` 使用独立上下文处理任务并返回最终答案，所需上下文必须包含在任务提示里。`read_only=true` 保持正常工具 schema，在执行时施加文件写入上限，包括 Shell。只读能力与操作系统沙箱分别设置。默认子代理不施加额外限制，但不能突破父代理权限上限。子 Agent 不递归创建子 Agent，默认最多 50 轮；即使最终失败，之前的工具调用仍可能有副作用。父子共享取消信号与 `budget.maxModelCalls` 模型调用预算。达到轮数上限、最终回答中断或最终回答为空时，会明确报告**未完成**，而不是成功。子代理失败时，父代理能收到有长度限制的近期工具错误原因及明确传递的诊断证据；成功时仍只返回最终回答。旧的 `type` 参数不再受支持，传入时会拒绝调用。参见[迁移指南](../guides/migrating-to-core-plugins.md#子代理能力迁移)。

## 后台任务

后台命令和子 Agent 共用任务管理器。`/jobs` 查看列表，`job_output` 读取输出，`job_kill` 取消任务。完成通知绑定原始对话，可插入正在运行的 Agent 或唤醒空闲 Agent，不会转移到界面当前选中的其他对话。

后台子 Agent 的完成通知包含完整的最终回答。后台命令的通知只包含已捕获输出的最后 1,200 个字符；可使用 `job_output` 读取更多内容。后台可调用任务若返回错误但没有结构化诊断，其任务记录会保留经过脱敏和长度限制的原始失败文本，而不是仅记录笼统状态。

客户端 `web_search` 和 `web_fetch` 的传输错误会标明异常类型（如 `ConnectError` 或 `ReadTimeout`），但不会显示可能包含私密 URL 或凭据的原始异常文本。

## MCP 与诊断

默认预设下，没有服务器配置就不启动 MCP 连接，也不注册 MCP 管理工具；显式启用后可在新增服务器前使用管理入口。

stdio MCP 服务器配置在用户或项目的 `mcp.json`，或 `mcpServers` 配置项中。参数和环境字段支持 `${VARIABLE}` 插值。工具名为 `mcp__<server>__<tool>`，禁用的条目不会启动。除非作者提供本地化，服务和扩展的描述保留第三方原文。

受管 MCP 调用遵守联网开关；服务无法约束本地文件范围时，需要明确的单次操作例外。
可信 MCP 服务的启动及自主活动在任务沙箱之外运行，离线预设不代表隔离了该服务进程。

`/mcp` 显示连接状态，`/mcp reconnect` 重新加载服务器配置。模型的 `mcp_status` 读取本地状态，`mcp_reconnect` 针对特定服务和已知错误执行有界、受权限约束的恢复。重连不会重放失败的业务操作。`diagnostic_get` 只暴露已授予调用 Runtime 的诊断证据，具体边界见[故障排查](../guides/troubleshooting.md)。

实现：[基础工具](../../../src/virgo/tools)、[官方插件](../../../src/virgo/plugins)、[扩展 API](../../../src/virgo/harness/extensions.py)。

## 已移除的产品功能

Schedule、Workflow、Team、持久 Tasks、通用 Memory 和 Goal 不再注册工具或命令，原有文件和历史工具结果保留。通过[迁移指南](../guides/migrating-to-core-plugins.md)检查旧数据，再按需要迁为项目脚本、CI 或外部系统。
