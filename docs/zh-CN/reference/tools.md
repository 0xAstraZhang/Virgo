# 工具与自动化

[文档首页](../README.md) · [English](../../en/reference/tools.md)

模型只会获得当前角色、配置和能力允许的工具。扩展、MCP 连接和网页路由可能改变实际列表。工具名、参数名和枚举值不随界面语言变化。

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

## 清单、提问与记忆

`todo_write` 使用传入的完整列表替换会话清单。状态为 `pending`、`in_progress` 和 `completed`，允许多个事项同时进行；输入不合法时保留旧列表。`/todos` 显示清单。未完成事项本身不会强制触发下一轮模型调用。

`ask_user` 允许主 Agent 提问，提供最多三个选项加自定义输入，或仅使用自由输入。非交互、取消和超时结果会报告 `answered=false`，不会编造答案。无人值守运行使用 `askUser.unattendedTimeout`；用户加入对话后可以撤销该超时。

`memory` 工具保存、更新或删除 Markdown 记忆。用户和反馈记忆存于用户目录，项目和参考记忆存于项目的 `.virgo/memory/`。初始系统提示只包含非空索引和读取入口。`/memory tidy` 使用小模型整理记录，替换失败时回滚。共享、持续维护的知识应放进项目文档，不应将密钥或临时任务状态保存为记忆。

## 子 Agent 与团队

`agent` 工具使用独立上下文处理明确任务，并返回最终答案。内置类型为 `general` 和只读 `explore`，自定义类型放在 `agents/*.md`。子 Agent 不递归创建子 Agent，默认最多 50 轮。即使最终响应失败，之前的工具调用也可能已经产生效果。

团队中的长期 Agent 各有独立上下文和进程内邮箱，完成当前委派后等待消息。`team_message` 用于委派或唤醒，单独修改任务 owner 不会唤醒队友。可选的独立 Git 工作树位于 `.virgo/worktrees/`。队友退出时会释放其认领但未完成的工作。`/team` 查看状态。

持久化任务图与会话清单分离。任务工具在 `.virgo/tasks/` 下创建、列出、读取和更新记录。`task_update(status="in_progress")` 显式认领工作，检查未完成依赖与归属，并拒绝依赖环。完成任务后会返回新解锁的工作。Unix 下变更使用进程间文件锁，Windows 实现使用当前进程内的线程锁。

## 后台任务、定时任务与目标

后台命令和子 Agent 共用任务管理器。`/jobs` 查看列表，`job_output` 读取输出，`job_kill` 取消任务。完成通知绑定原始对话，可插入正在运行的 Agent 或唤醒空闲 Agent，不会转移到界面当前选中的其他对话。

定时任务在 Virgo 运行期间投递保存的提示。手动创建、持久化默认值、时区和错过执行时刻的行为见[定时任务](../guides/schedules.md)。

`/goal <condition>` 显式启用完成条件循环。主 Agent 准备停止时，无工具评审根据现有对话证据判断该条件，可以确认完成、判定不可能或要求继续。后台任务仍在运行时会等待。连续未满足达到 `goal.maxContinues`，或评审失败时，将控制权交还用户并保留目标。`/goal clear` 清除目标。

## 工作流

工作流由 Python 脚本使用 `ctx.agent`、`ctx.map`、`ctx.parallel`、`ctx.phase` 和 `ctx.log` 定义可重复的编排，模型负责启动脚本。成功的 Agent 调用记录在 `.virgo/runs/`；`/workflow resume <run_id>` 从头运行脚本，并复用匹配的已完成 Agent 调用。

只复用已记入日志的 Agent 调用。脚本中的其他 Python 副作用在续跑时可能再次执行，编写时需要考虑重放。工作流名称和描述在工作空间会话初始化时建立索引；新增或改名后需要重启，或重新初始化工作空间会话。执行时会重新加载选中的脚本。详见[扩展](../development/extensions.md#工作流)。

## MCP 与诊断

stdio MCP 服务器配置在用户或项目的 `mcp.json`，或 `mcpServers` 配置项中。参数和环境字段支持 `${VARIABLE}` 插值。工具名为 `mcp__<server>__<tool>`，禁用的条目不会启动。除非作者提供本地化，服务和扩展的描述保留第三方原文。

`/mcp` 显示连接状态，`/mcp reconnect` 重新加载服务器配置。模型的 `mcp_status` 读取本地状态，`mcp_reconnect` 针对特定服务和已知错误执行有界、受权限约束的恢复。重连不会重放失败的业务操作。`diagnostic_get` 只暴露已授予调用 Runtime 的诊断证据，具体边界见[故障排查](../guides/troubleshooting.md)。

实现：[基础工具](../../../src/virgo/tools)、[内置功能](../../../src/virgo/features)、[扩展 API](../../../src/virgo/harness/extensions.py)。
