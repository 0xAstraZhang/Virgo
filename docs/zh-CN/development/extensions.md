# 扩展 Virgo

[文档首页](../README.md) · [Core 边界](core-plugin-boundaries.md) · [English](../../en/development/extensions.md)

## 选择合适的扩展入口

| 资源 | 用户位置 | 项目位置 | 对应能力 |
| --- | --- | --- | --- |
| Python 扩展 | `~/.virgo/extensions/*.py` | `.virgo/extensions/*.py` | 可信的 `setup(api)` 代码 |
| 技能 | `~/.virgo/skills/<name>/SKILL.md` | `.virgo/skills/<name>/SKILL.md`，然后 `.agents/skills/<name>/SKILL.md` | `skills` |
| 提示模板 | `~/.virgo/prompts/*.md` | `.virgo/prompts/*.md` | Core 提示装配 |
| 子 Agent 类型 | `~/.virgo/agents/*.md` | `.virgo/agents/*.md` | `subagent` |
| MCP 服务器 | `~/.virgo/mcp.json` | `.virgo/mcp.json` | `mcp` |

`VIRGO_HOME` 替换用户目录。声明式资源先加载用户级，再加载项目级，同名项以较后的定义为准。官方插件位于 `virgo.plugins`，与 Python 扩展共用 `setup(api)`。插件按固定顺序装载一次；重启或初始化工作区会话后重新发现文件。不提供运行时卸载、依赖安装器或插件沙箱。

项目 Python 扩展需要项目配置之外的信任：为所选项目使用 `--trust-project-extensions`，或配置用户级 `extensions.trustedProjects`。授权前审查代码。用户级扩展属于用户控制的可信代码。`--no-extensions` 跳过这两类 Python 扩展，不关闭官方插件；`minimal` 关闭官方预设能力，但可信 Python 扩展仍独立加载；同时使用 `--profile minimal --no-extensions` 才关闭两者。详见[迁移](../guides/migrating-to-core-plugins.md#信任项目扩展)。

## 添加工具与命令

创建并审查 `.virgo/extensions/project_overview.py`，再以 `--trust-project-extensions` 启动：

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
            description="List names directly inside the working directory.",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=overview,
            read_only=True,
        ),
        roles=("main", "subagent"),
    )
    api.register_command(
        "location", lambda _session, _args: api.ui.print(str(api.cwd)),
        "show the working directory", concurrent=True,
    )
```

`execute(args, ctx)` 返回 `ToolResult` 或字符串。`ToolContext` 提供 `cwd`、`call_id`、`signal`、`agent`、可选进度回调和绑定的审查来源。执行时应响应取消。`content` 是模型可见内容；`details` 是结构化状态／UI 数据。已知失败返回 `ToolResult.error(...)`，其他异常交由 Core 保留类型与堆栈。

`AgentTool` 支持 `sequential`、`prepare_arguments` 和本地化元数据。工具名、参数键与枚举值是协议契约，不应翻译。`i18n_key`、`api.t`、`ctx.t` 见[本地化](localization.md)。

命令为兼容保留 `(session, args: str)` 回调签名；新代码可使用捕获的 API，不依赖 Session 内部状态。只有能够在 Agent 运行时安全执行的命令才设置 `concurrent=True`。`argument_hint="[path]"` 提供编辑器提示，`description_key` 和 `argument_hint_key` 提供本地化元数据。

## 注册、发布与清理

工具、命令和事件处理器都有所属插件。重复的工具／命令名称明确失败，包括与 Core 或其他插件冲突；提示章节按插件归属命名。可选插件初始化失败会记录诊断并回滚部分注册，不留下只装配一半的能力。Core 初始化或权限失败会阻断执行。

`api.replace_tools(namespace, [(tool, roles), ...])` 验证整个候选集后，原子替换一个属于本插件的动态工具集。它不能占用其他插件的命名空间或覆盖别人的工具。已有请求和工具批次保持快照；每个 Runtime 在下一次模型请求边界看到新 Schema。传输专用的旧句柄可以保守失败，不重放副作用。MCP 通过此接口发布，不再修改 `session._tools`。

获得资源后立即注册清理：

```python
def setup(api):
    connection = open_connection()  # 由该扩展提供
    api.on_cleanup(connection.close)
    # 注册清理后，再注册使用该连接的工具。
```

`on_cleanup(callback)` 在关闭或失败回滚时按注册逆序执行。清理异常进入诊断，不阻止其他回调。关闭操作应有界、可重复，并尊重所创建任务的取消信号。

## Runtime 与诊断服务

| 公开 API | 用途 |
| --- | --- |
| `api.settings`、`api.paths`、`api.cwd`、`api.ui`、`api.t` | 生效配置、资源路径和 Host 服务 |
| `api.current_runtime()`、`api.current_store()` | 在命令或初始化入口捕获当前归属 |
| `api.runtime_for_context(ctx)` | 解析工具真实调用方，不借用当前选中的对话 |
| `api.create_runtime(name, role, parent=..., permission=..., ...)` | 创建继承权限上限、记账、取消和共享请求预算的子运行时 |
| `api.release_runtime(runtime)` | 释放完成后的子运行时注册及关联权限状态 |
| `api.tools_for(role)`、`api.resolve(spec)` | 解析角色工具和模型配置 |
| `api.authorize_tool(event)` | 为适配操作应用 Core 预检、钩子和执行前权限门 |
| `api.notify(..., store=owner, diagnostics=...)` | 将异步结果送回原对话 |
| `api.report_error(..., store=owner)`、`api.retain_diagnostics(owner, records)` | 保留归属明确的诊断证据 |
| `api.complete(...)` | 使用 Core 请求策略发起记账明确的辅助模型请求 |

`runtime_for_context` 验证真实 Runtime、所属 Session、原始审查来源对象和 Agent 名称；非法或外来来源返回 `None`，请求应明确失败。解析 Runtime 不授予其他证据访问权。工具调用应通过该入口解析身份，不能改用 `current_runtime()`；后者用于在入口刻意捕获当前对话。

子 Runtime 工厂接收显式可信父对象，防止子任务提高父运行时的权限等级。配置后，`budget.maxModelCalls` 限制与子任务共享的模型请求数；`subagents.maxTurns` 仍限制单个聚焦子任务的轮数。插件不应另建独立 AgentSession 来绕过归属或预算。

`api.services` 仅用于明确的共享服务契约。`api.session` 在 0.2 系列作为兼容入口保留，官方插件使用 facade。不要修改 `_tools`、`_lock`、`main` 或 UI 选择状态。保留能力的 `virgo.features` 导入路径同样只是临时别名，需在 0.3 前迁移。

## 事件与权限

`api.on(event, handler, priority=0)` 返回取消订阅函数；优先级高的先执行，同优先级保持注册顺序。事件签名保留：

| 事件 | 处理器契约 |
| --- | --- |
| `session_start`、`session_shutdown` | `(session)` |
| `input` | `(text, session)` → 替换文本、`Handled` 或 `None` |
| `user_steer` | `(text, session)` |
| `before_agent_start` | `(runtime, prompts)` |
| `agent_event`、`turn_end` | `(event, runtime)` |
| `tool_call` | `(event, session)` → `Block` 或 `None`；可调整参数 |
| `tool_result` | `(result, event, session)` → 替换结果或 `None` |
| `context` | `(messages, runtime)` → 请求视图或 `None` |
| `agent_stop` | `(runtime)` → `Continue` 或 `None` |
| `agent_end`、`before_compact` | `(runtime)` |
| `compact` | `(runtime, summary)` |

`Block(reason)` 阻止工具；只有明确启用的继续策略才使用 `Continue(text)`。处理器失败进入诊断并与后续处理器隔离。上下文视图应保留追加式的消息前缀。

`api.register_permission_policy(policy)` 在必要的 Core 策略后添加限制，不能替换 Core 权限门。策略实现 `preflight(event)` 和 `before_execute(event)`，第二次检查捕获预检后的变化。托管／适配操作需使用 `api.authorize_tool`；`read_only` 标记不是授权。Python 本身仍是可信代码，不在沙箱中执行。

权限模式与辅助函数位于 `virgo.harness.permissions`；具体权限门、范围判断和审查来源契约同样属于 Harness。`auto_review` 通过 `register_permission_reviewer` 提供可选模型审查；禁用它仍保留规则和不确定操作的人工审批。

配置命令钩子支持 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`，从 stdin 读取 JSON。退出码 `2` 表示相应事件的阻止／继续，stderr 提供反馈；其他失败记录诊断。匹配与准确语义见 [`extensions.py`](../../../src/virgo/harness/extensions.py)。

## 提示章节与状态

`api.add_system_section(name, fn, order=50)` 调用 `fn(role)`。`add_runtime_system_section(name, fn, order=50)` 提供候选 `RuntimeContext`，包含角色、cwd、Store、记账 Store 和审查父来源。构造失败会拒绝候选，不应临时替换当前会话状态。

`api.append_entry(kind, data)` 和 `api.entries(kind)` 访问当前 Store 的自定义状态。后台工作需先捕获真实所有者，再使用其 Store 和 `api.notify(..., store=owner)`。保存失败是真正的执行边界，不能当作持久化成功。Todo 仍是当前对话状态，不是持久任务图。

## 技能、模板和 Agent 类型

技能包含 YAML frontmatter（`name`、`description`）和 Markdown。发现阶段展示索引，模型按需读取正文；`/skill:name args` 显式展开，内部相对路径按技能目录解析。

`prompts/review.md` 模板成为 `/review`：

```markdown
---
description: review the current diff
---
Review the current diff. Focus on: ${1:-correctness}. Additional context: $@
```

模板支持 `$1`、`$@`／`$ARGUMENTS`、`${1:-value}`、`${@:2}`；没有占位符时，参数追加到正文。子 Agent frontmatter 支持 `name`、`description`、`tools`、`model`、`permission`，正文提供额外系统指令。[审查 Agent 示例](../../../examples/project/.virgo/agents/reviewer.md)选择只读工具和计划模式。

捕获异常后用 `virgo.diagnostics.capture_error` 保留结构化诊断；成功恢复时将原始证据保留在 `related_errors`。属于某个 Runtime 的错误优先使用其 `report_error()`；结果／通知的显式传播可以授予父任务访问权，单独的错误 ID 字符串不能。详见[故障排查](../guides/troubleshooting.md)。
