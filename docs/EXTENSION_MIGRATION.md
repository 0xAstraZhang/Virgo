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
