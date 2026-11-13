# Harness 简化：扩展迁移

本次改动不提供兼容开关。会话 JSONL、记忆 Markdown、任务 JSON 文件格式及 `features.*` 配置不变，无需数据迁移。

## 工具定义

`AgentTool.snippet` 已删除。移除构造函数的 `snippet=` 参数，把调用所需的说明集中在 `description`；system 不再生成第二份工具目录。使用位置参数传递可选字段的扩展，应改用字段名，避免删除字段后的位移。

```python
api.register_tool(AgentTool(
    name="lookup",
    description="Look up a record by its exact ID.",
    parameters={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    execute=lookup,
    read_only=True,
))
```

搜索结果中的 `snippet` 字段不变。当前默认主 Agent 在空项目、默认功能和客户端网页工具配置下提供 28 个工具，包含 `diagnostic_get`、`mcp_status` 和 `mcp_reconnect`；角色与 `features.*` 开关继续生效。没有引入通用动态工具发现层，MCP schema 的改变只在安全请求边界生效。

## 任务认领与唤醒

`TaskStore.claim_next` 已删除。扩展需自行选择具体任务，再调用现有显式更新接口；依赖检查和锁仍由 TaskStore 执行：

```python
tasks = api.services["tasks"]
task, newly_ready = tasks.update(task_id, "alice", status="in_progress")
```

模型通过 `task_list` / `task_update` 执行相同操作。队友不会在空闲时领取遗留任务；设置 owner 只修改记录。需要队友处理工作时，另外用 `team_message` 发消息。停止队友仍释放其未完成的进行中任务。

Todo 不再限制一个进行项或 50 项，也不再通过停止钩子催促模型继续。依赖旧提醒的扩展应自行表达明确的用户工作流；显式 Stop hook、`/goal`、Workflow 和 `plan_first` 仍可用。

## 提问结果和 UI

所有未回答路径都不再提供 `choice` 或 `answer`。消费者应先检查 `details["answered"]`；超时另有 `timed_out=true`，推荐选项只是展示信息，不代表选择或授权。历史记录不改写，读取旧记录也应以 `answered` 为准。

UI 新增可选实现 `cancel_input_timeouts()`，用于用户发来 steering 后撤销当前问题的无人值守时限；基类默认无操作。内置全屏与 plain UI 均已实现。自定义 UI 若支持无人值守超时，也应实现此方法，保持表单打开且不填入答案。

## 摘要

通用摘要不再要求固定 Markdown 标题；读取会话的扩展应把摘要当作续接文本，不按标题解析。已有工具文本先完整序列化，再按总预算保留首尾并标记遗漏。工具调用与结果配对、原生压缩签名和上下文绑定、失败不覆盖历史的机制不变。


## 持久化故障、权限策略与 Runtime 构造

### 保存失败

`SessionStore.append()` 在写入失败后仍保留逻辑记录，并抛出 `SessionSaveError`；不要捕获后当作已保存。异常的 `entry` 是本次记录，`store.save_error` 是首次故障。之后所有追加都只进入内存并抛出同类异常，不自动重试磁盘。主动内存会话和已回收 store 的 `path=None` 不触发故障。

自定义 UI 可以实现 `save_failed(session_id, path, message)`；默认回退到 `print(message, "error")`。不要把保存错误提示再写回故障存储。JSON 客户端应处理 `session_save_failed`；即使已收到模型最终答案，也应检查进程退出码。

### 权限策略

自定义策略通过 `api.register_permission_policy(policy)` 绑定，提供 `preflight(event)` 和 `before_execute(event)`，返回 `Block | None`。`api.services["permissions"]` 仍指向绑定实例，但直接赋值该字典不再替换执行策略。需要兼容内置权限命令和队友模式的策略还应保留内置权限服务的模式管理接口；最简单的方式是包装原策略并转发这些属性。

预检保留原优先级 100 的注册位置。高优先级扩展仍先执行，同优先级按注册顺序运行，第一次非空结果短路。一次派发使用同一处理器快照，动态添加或移除回调只影响下一次派发。执行前复核位于实际工具任务内部；任何检查异常都会阻止工具执行。

### 候选 Runtime 的系统提示

旧 `add_system_section(name, fn)` 继续传入角色字符串；`api.entries()` 和 `api.session.store` 始终表示活动会话。读取待激活会话的扩展改用显式上下文：

```python
def setup(api):
    def target_notes(context):
        if context.role != "main" or context.store is None:
            return None
        notes = context.store.custom("project_notes")
        return "\n".join(str(note) for note in notes)

    api.add_runtime_system_section("target_notes", target_notes, order=50)
```

`RuntimeContext` 包含 `role`、`cwd`、`store`、`accounting_store` 和 `review_parent`；字段不可重新赋值，存储引用只供读取，不得在准备阶段写入活动会话或候选记录。新回调异常会使准备失败，原活动会话保持不变。旧、新回调按 `order` 排序。

内置主 Agent、队友和子 Agent 统一通过 `session.create_runtime(name, role, **kwargs)` 创建。扩展排队后台任务前应捕获 `ctx.review_source`，传作 `review_parent`；不要在后台启动时重新读取活动主 Agent。子 Agent 的 `store=None` 仍表示不创建独立消息 JSONL，辅助请求必须传入 Runtime 的 `accounting_store`。

## 结构化错误诊断

`AssistantMessage.error_details`、`Block.details` 和 `AgentConfig.on_error` 均为新增可选字段；旧会话无需迁移。Provider 的 `stop_reason`、`error_kind` 和工具的 `is_error` 继续决定执行行为，消费者不要用错误消息文本推断重试策略。

自定义工具直接抛异常即可由 Agent 记录阶段、堆栈和调用 ID。若工具必须自行捕获异常，应在捕获点使用 `virgo.diagnostics.capture_error()` 并返回 `ToolResult.error(..., details={"error": details})`；只返回字符串会失去原始堆栈。不要把整个异常 `__dict__`、请求对象、headers 或工具参数作为诊断上下文。`message=` 可替换嵌入完整响应正文的 SDK 异常文本。

后台扩展使用 `session.report_error(error, phase="extension.operation", store=original_store, context={...})`。在排队时捕获 `original_store`；切换会话后仍将错误绑定到原任务。报告入口复用 `_virgo_error_details`，被多个边界捕获的同一错误不会重复写入。错误恢复钩子可返回成功结果，原错误会保留在 `details["related_errors"]`。

独立使用 `EventBus` 的调用方可传 `on_diagnostic(details, args)`；旧 `on_error(str)` 继续兼容。独立使用 `Agent` 时可传 `AgentConfig(on_error=...)`。没有接入持久化回调或回调本身失败时，诊断会回退到 stderr。扩展失败的详情默认可用，不再依赖 `VIRGO_DEBUG`。

## 模型诊断视图与恢复边界

### 工具结果投影

`AgentConfig.project_tool_result(call, result) -> list` 是新增可选回调，独立 Agent 默认不配置。它在结果钩子结束后、`ToolResultMessage` 入历史前调用一次，只负责生成内容视图，不做 I/O、不改变 `is_error` / `terminate`、不再次执行工具。Harness 默认投影保留原文本和图片，并为最终错误追加最多 2048 UTF-8 字节的白名单摘要。投影收到隔离副本，异常或非法返回不会修改原结果；旧消息不会在后续请求中被重新投影。

`ToolResult.details` 仍不被 Provider 自动序列化。不要把任意业务对象写入 `details` 后假定模型会看到；需要模型读取的普通业务结果继续放 `content`，错误证据使用 `details["error"]` 和 `related_errors`。结果钩子将失败恢复为成功时，诊断保留供查询，但不再给成功结果追加失败摘要。

正常成功结果的非字典 `details`（例如扩展自己的 list 载荷）保持原类型，不强制增加执行元数据。使用字典时，`details["execution"]` 由 Agent 记录真实执行边界，包括 `started`、`outcome`、`side_effects`、`result_hook`。适配器可补充独立的 `details["transport"]`；目前 MCP 使用 `protocol/server/generation/dispatch/outcome/side_effects`。Agent 在结果钩子前取快照，钩子不能把已执行操作改成未执行，也不能因替换字典结果丢失原传输事实。工具返回成功后钩子失败不表示原操作没发生；`read_only` 也不是幂等契约。

`features.diagnostics=false` 关闭模型诊断工具、结果投影和自动诊断通知，独立日志仍工作。显式限制 Runtime 工具名单时不会自动补入 `diagnostic_get`；`diagnostic_summary(..., query_available=False)` 与 `project_tool_result(..., query_available=False)` 仍保留错误 ID，但标记查询工具未暴露，不生成可调用该工具的提示。

### Runtime 范围与异步传播

每个 Runtime 的 `diagnostic_access` 只接受显式授予的 ID，默认保存 100 条证据和 1000 个授权 ID；`diagnostic_get` 一页完整响应最多 8 KiB。该工具通过 `ctx.review_source.runtime` 的真实对象、Session、来源身份和 Agent 名核对调用者，不依据当前选中的主聊天。共享 `accounting_store` 不会自动授予兄弟或子 Runtime 读取权限，记录文本里的 ID 也不构成授权。

`session.report_error(...)` 负责独立日志，不应作为跨 Runtime 的隐式共享机制。已绑定 Runtime 的扩展可用 `runtime.report_error(...)` 保留其诊断引用；工具返回 `details["error"]` / `related_errors` 时，Harness 会在实际交付时建立相应授权。`runtime.report_error(..., notify=True)` 同时排队供下一次正常请求展示，但不会自行唤醒或发起模型调用。

`ExtensionAPI.notify()` 和 `AgentSession.notify()` 增加可选关键字 `store=` 与 `diagnostics=`；旧调用形式继续兼容。后台扩展应在排队前保存原 `store` / Runtime，完成时把证据和消息交回该归属。若通知本身可能失败，先保留诊断：

```python
# original_store 和 bound_runtime 在排队时捕获。
record = bound_runtime.report_error(error, phase="extension.worker", notify=False)
session.retain_diagnostics(original_store, [record])
api.notify("Background operation failed.", store=original_store, diagnostics=[record])
```

`retain_diagnostics` 只保留待处理证据，不创建模型轮次。所属分支的 `runtime_diagnostic` 和 `diagnostic_checkpoint` 自定义记录支持恢复与压缩后引用；消费者不能把“已展示”理解为“故障已修复”。用户取消和保存失败仍阻止后续调度，扩展不能借诊断投递续跑原本已停止的任务。

### MCP 与重试

`services["mcp"]` 保持原来的服务名到 client 字典；不要在工具闭包或后台扩展中永久缓存其 client，定向重连会替换连接对象。普通 MCP 工具通过稳定服务句柄派发，schema 不变时不需要重建工具对象。`services["mcp_refresh"](runtime) -> bool` 由 Harness 在完整工具批次之后、下一模型请求之前调用；返回真表示当前 Runtime 需要刷新工具视图。显式限制的 Runtime 工具名单和其专属工具继续保留。

MCP schema 与包装器先准备、检查名称冲突，再发布；候选失败不会移除旧工具。旧 schema 的包装器不得悄悄套用新契约。`mcp_reconnect` 不设 `read_only=True`，不改权限模式、不重读配置命令，也不自动重放失败调用。调用者必须保留 `generation` 和实际发送状态；写入错误、超时及响应丢失不能推导成“未执行”。每服务最多两次无远端工具成功的恢复尝试，初始化分页有单次握手 deadline 和重复游标/页数保护。

Provider 的退避仍只有 `Runtime._attempts` 一处。新增 `retry.maxWaitSeconds` 默认 120，限制一次逻辑模型调用的累计重试等待；有效 `Retry-After` 秒数或 HTTP 日期作为最短等待，超过剩余预算就停止。扩展不要在诊断投影、通知或工具恢复层再套一层模型请求重试。

详细查询状态、数据边界与测试入口见 [错误诊断](ERRORS.md)。
