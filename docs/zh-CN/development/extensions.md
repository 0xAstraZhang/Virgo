# 扩展 Virgo

[文档首页](../README.md) · [English](../../en/development/extensions.md)

## 选择合适的扩展入口

| 资源 | 用户位置 | 项目位置 |
| --- | --- | --- |
| Python 扩展 | `~/.virgo/extensions/*.py` | `.virgo/extensions/*.py` |
| 技能 | `~/.virgo/skills/<name>/SKILL.md` | `.virgo/skills/<name>/SKILL.md`，然后 `.agents/skills/<name>/SKILL.md` |
| 提示模板 | `~/.virgo/prompts/*.md` | `.virgo/prompts/*.md` |
| 子 Agent 类型 | `~/.virgo/agents/*.md` | `.virgo/agents/*.md` |
| 工作流 | `~/.virgo/workflows/*.py` | `.virgo/workflows/*.py` |
| MCP 服务器 | `~/.virgo/mcp.json` | `.virgo/mcp.json` |

`VIRGO_HOME` 可替换用户目录。资源先加载用户级再加载项目级，同名的声明式资源采用较后的定义；Python 扩展按加载顺序执行。重启或初始化工作空间会话后会重新发现文件。扩展是在 Virgo 进程内执行的受信任 Python 代码。

## 添加工具与命令

创建 `.virgo/extensions/project_overview.py`：

```python
from virgo.agent import AgentTool, ToolResult


def setup(api):
    def overview(args, ctx):
        names = sorted(path.name for path in ctx.cwd.iterdir())
        return ToolResult.text("\n".join(names))

    api.register_tool(
        AgentTool(
            name="project_overview",
            label="Project overview",
            description="List the names directly inside the working directory.",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=overview,
            read_only=True,
        ),
        roles=("main", "subagent"),
    )

    def location(session, args):
        api.ui.print(str(session.cwd))

    api.register_command(
        "location", location, "show the working directory", concurrent=True
    )
```

`execute(args, ctx)` 返回 `ToolResult` 或字符串。`ToolContext` 提供 `cwd`、`call_id`、`signal`、`agent`、可选进度回调和绑定的审查来源。执行时应响应取消。`content` 是模型可见内容，`details` 是结构化状态／UI 数据。已知失败可使用 `ToolResult.error(...)`，也可直接抛出异常，让 Harness 保留类型和堆栈。

`AgentTool` 还支持 `sequential`、`prepare_arguments` 和本地化元数据。工具名与 schema 是公开契约，不应翻译名称、属性键、枚举值或可执行示例。`i18n_key`、`api.t` 和 `ctx.t` 见[本地化](localization.md)。

命令接收 `(session, args: str)`。只有能够在 Agent 忙碌时安全读取状态的命令才使用 `concurrent=True`。编辑器参数提示由独立的 `argument_hint="[path]"` 提供，不应从说明文本解析。扩展还可用 `description_key` 和 `argument_hint_key` 提供本地化元数据。

## 事件与策略

`api.on(event, handler, priority=0)` 注册处理器并返回取消订阅函数。优先级高的先执行，同优先级保持注册顺序。常用事件包括：

| 事件 | 处理器契约 |
| --- | --- |
| `session_start`、`session_shutdown` | `(session)` |
| `input` | `(text, session)` → 替换文本、`Handled` 或 `None` |
| `user_steer` | `(text, session)` |
| `before_agent_start` | `(runtime, prompts)` |
| `agent_event`、`turn_end` | `(event, runtime)` |
| `tool_call` | `(event, session)` → `Block` 或 `None`，可调整参数 |
| `tool_result` | `(result, event, session)` → 替换结果或 `None` |
| `context` | `(messages, runtime)` → 请求视图或 `None` |
| `agent_stop` | `(runtime)` → `Continue` 或 `None` |
| `agent_end`、`before_compact` | `(runtime)` |
| `compact` | `(runtime, summary)` |

用 `Block(reason)` 阻止工具，用 `Continue(text)` 实现用户显式启用的继续策略。处理器失败会记录诊断，不阻止后续处理器。上下文视图应保持旧消息前缀稳定。

权限策略通过 `api.register_permission_policy(policy)` 注册，实现 `preflight(event)` 和 `before_execute(event)`。由于预检后参数或授权仍可能改变，第二次检查不可省略。使用绑定到 Runtime 的审查来源，不应按当前界面选中的会话选择授权历史。

权限模式常量 `MODES`、规范化函数 `normalize_permission_mode`、显示名称 `permission_mode_label` 和计划模式工具筛选 `plan_tool_allowed` 统一从 `virgo.harness.permissions` 导入；`virgo.features.permission` 保留兼容导出。`permission_mode_label(mode)` 默认返回英文，传入 `permission_mode_label(mode, session.t)` 则使用会话语言，规范化后的模式标识不变。

最小权限协议未覆盖所有内置功能：hosted web、子 Agent 和队友还使用 `on_tool_call`、`mode_for`、`decide`、`set_agent_mode` 等策略服务。需要这些功能时，包装并转发内置策略。仅赋值 `api.services["permissions"]` 不会替换已注册的执行策略。详见[策略能力边界](abstraction-boundaries.md#自定义权限策略的能力范围)。

配置中的命令钩子支持 `PreToolUse`、`PostToolUse`、`UserPromptSubmit` 和 `Stop`，从 stdin 读取 JSON。退出码 `2` 是控制结果，按事件表示阻止或继续，stderr 作为反馈；其他失败进入诊断。匹配与具体语义见 [`extensions.py`](../../../src/virgo/harness/extensions.py)。

## 提示段与持久化

`api.add_system_section(name, fn, order=50)` 调用 `fn(role)`。需要候选 Runtime 的数据时，使用 `add_runtime_system_section(name, fn, order=50)`，其 `RuntimeContext` 提供角色、cwd、存储、记账存储和审查父来源。构造过程中不要临时替换活动会话状态。

`api.append_entry(kind, data)` 保存自定义状态，`api.entries(kind)` 从活动分支恢复。保存失败是真实的执行边界，不能当成追加成功。后台回调应捕获原始 `store`，并调用 `api.notify(..., store=owner)` 或 `session.report_error(..., store=owner)`，避免切换对话后输出被转向。

按调用方查询诊断或恢复连接时，使用 `session.runtime_for_context(ctx)`。它核对真实 Runtime 类型、所属 Session、原始 `review_source` 对象和 Agent 名称；不可信来源返回 `None`，具体功能负责通过 `ctx.t` 返回本地化拒绝结果。会话切换不改变旧调用的归属，解析 Runtime 也不授予额外诊断访问权。

## 技能、模板与 Agent 类型

技能使用含 `name`、`description` 的 YAML frontmatter，之后是 Markdown 指令。发现阶段只展示名称、描述和位置，模型按需读取正文。`/skill:name args` 显式展开技能，内部相对引用按技能所在目录解析。

`prompts/review.md` 等文件会成为 `/review` 命令：

```markdown
---
description: review the current diff
---
Review the current diff. Focus on: ${1:-correctness}. Additional context: $@
```

模板支持位置参数 `$1`、全部参数 `$@`／`$ARGUMENTS`、默认值 `${1:-value}` 和切片 `${@:2}`。没有占位符时，传入参数追加到模板正文末尾。

子 Agent Markdown 的 frontmatter 支持 `name`、`description`、`tools`、`model` 和 `permission`，正文作为附加系统提示。[审查 Agent 示例](../../../examples/project/.virgo/agents/reviewer.md)使用计划模式和显式的只读工具列表。

## 工作流

工作流导出 `META` 和 `run(ctx, args)`。`ctx.agent(prompt, schema=..., tools=..., label=...)` 运行子 Agent，`ctx.map` 和 `ctx.parallel` 协调任务，`ctx.phase`／`ctx.log` 报告进度。结构化结果按指定 schema 校验。

日志按调用内容缓存成功的 Agent 结果。续跑会从头执行 Python 控制流，日志范围之外的副作用应设计为可重复，或显式保护。工作流元数据在工作空间初始化时发现一次，执行时重新加载选中的脚本。[审查工作流示例](../../../examples/project/.virgo/workflows/review-changes.py)展示了并行审查后再验证。

## 错误与证据

捕获异常后，可用 `virgo.diagnostics.capture_error` 保留结构化诊断，再放入 `ToolResult.details["error"]`。若恢复成功，原始错误保留在 `related_errors`。诊断上下文应限制为 ID、计数和安全元数据，不要附加任意请求对象。

证据属于某个 Runtime 时，使用该 Runtime 的 `report_error()`。只有结果或通知中的显式传播才授予父任务证据，单独传递错误 ID 字符串并不足够。详见[故障排查](../guides/troubleshooting.md)和[扩展 API 源码](../../../src/virgo/harness/extensions.py)。
