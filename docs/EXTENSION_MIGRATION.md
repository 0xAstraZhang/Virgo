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

搜索结果中的 `snippet` 字段不变。默认主 Agent 的 24 个工具及现有角色可见性保留，没有引入动态工具发现层。

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
