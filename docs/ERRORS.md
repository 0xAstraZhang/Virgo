# 错误诊断

发生故障时，应能回答：哪次运行、哪个 Agent、哪个阶段失败，根因是什么，是否重试过，以及去哪里读取证据。Virgo 为这些问题提供统一错误记录，并把诊断写入独立于会话历史的日志。

## 如何排查

1. 记录界面显示的错误 ID。错误提示包含阶段、异常类型、简短原因和 `Diagnostics:` 路径。
2. 在当前运行实例输入 `/diagnostics` 查看最近十条错误和日志路径；输入 `/diagnostics <error-id>` 查看完整 JSON。支持唯一的 ID 前缀；查询范围为当前实例保留的最近一百条记录。
3. 重启后直接读取提示中的 JSONL 文件；如果已轮转，同时检查同名 `.1`、`.2` 文件。每行是一条独立 JSON 记录，可按 `error_id`、`context.run_id` 或 `context.tool_call_id` 检索。
4. 模型失败先看 `kind` 与 `context.http`，本地失败先看 `phase`、`exception_type`、`cause_chain` 和 `traceback`。重试失败再成功的记录仍在日志中。

日志位于 `~/.virgo/logs/harness-<UTC时间>-<进程号>-<随机ID>.jsonl`。设置 `VIRGO_HOME` 后使用其 `logs/` 子目录。日志在首次错误时创建，不需要 debug 开关。多个全屏会话实例各有日志；同一实例执行 `/new` 后继续使用原日志，以每条记录的 `session_id` 区分归属。

`--no-session` 只关闭对话历史持久化，故障仍会生成诊断日志。独立日志避免保存会话失败时丢失最后的故障原因。

## 记录结构

以下为字段示意，实际 HTTP 和上下文字段按来源补充：

```json
{
  "schema_version": 1,
  "error_id": "<唯一错误ID>",
  "timestamp": "<UTC时间>",
  "code": "PROVIDER_STREAM_FAILED",
  "phase": "provider.stream",
  "kind": "retryable",
  "message": "Provider HTTP 429: rate limited",
  "exception_type": "<SDK异常类型>",
  "traceback": "<文件、行号、函数和异常链>",
  "cause_chain": [],
  "context": {
    "session_id": "<原会话ID>",
    "run_id": "<运行ID>",
    "model_call_id": "<逻辑模型调用ID>",
    "agent": "main",
    "model": "<provider/model>",
    "api": "openai",
    "purpose": "agent",
    "attempt": 1,
    "max_retries": 3,
    "duration": 0.25,
    "http": {
      "status_code": 429,
      "request_id": "<服务端request ID>",
      "retry_after": "2",
      "endpoint": "https://api.example/v1/chat/completions"
    }
  },
  "log_path": "<诊断日志路径>"
}
```

| 字段 | 含义 |
|---|---|
| `error_id` | 一次故障的稳定标识；传递到父工具、会话和 CLI 时复用 |
| `code` / `phase` | 机器可读代码与发生阶段；普通异常使用阶段派生的 `*_FAILED`，协议或 hook 故障使用专门代码 |
| `kind` | 保留既有 `retryable`、`overflow`、`fatal` 等分类；预期权限阻止使用 `blocked` |
| `exception_type` | 实际异常类型；仅有错误文本时为 `null` |
| `cause_chain` | 从外层异常向根因排列，包含 `cause` / `context` 关系和每层的堆栈位置 |
| `traceback` | 可读堆栈；不包含 frame locals 或源代码行 |
| `context` | 白名单元数据，例如会话、Agent、模型、工具、调用 ID、耗时、退出码和 HTTP 信息 |
| `log_path` | 成功记录时的诊断日志路径；轮转后按同名文件族检索 |
| `log_pattern` / `log_expired` | 文件族检索模式与轮转淘汰标记；最近一百条记录仍可通过 `/diagnostics` 查看 |
| `record_truncated` | 单条记录超出文件预算后使用了明确裁剪的摘要 |
| `logging_error` | 诊断日志未能保存的原因；完整原始诊断同时尝试输出到 stderr |

HTTP 状态码、request ID 和 Retry-After 只在服务或 SDK 提供时出现。网络错误仍可以显示简短连接提示，但详情保留实际超时、断连或协议异常及其 cause。SDK 异常的完整 response body 不会直接序列化；只提取错误消息和允许的传输字段。

## 各边界的行为

| 边界 | 保留的证据与执行行为 |
|---|---|
| 模型请求与重试 | 每次失败记录真实模型、调用 ID、尝试序号和耗时；成功不会删除前次失败。原有退避、回退模型及 overflow 压缩策略继续决定恢复行为 |
| 辅助请求与压缩 | `model_call` 增加 `error_kind`、`error_message`、`error_id` 和 `error_details`；内部提示词与回答不复制到诊断 |
| 子 Agent 与队友 | 记录 Agent 名称、原会话和记账归属；父工具传播已有诊断 ID |
| 工具 | 区分 `tool.prepare`、`tool.preflight`、`tool.permission`、`tool.execute`、`tool.result_hook`；保留工具名与调用 ID |
| 权限策略 | 异常仍阻止执行，保留 `permission.preflight` / `permission.before_execute` 根因；不把策略错误当作允许 |
| 扩展 | 导入、系统提示段、事件 handler 和命令 hooks 出错均可追查。一个 handler 失败不阻止后续 handler |
| 命令 hooks | 记录事件、序号、退出码或 timeout 和有限的 stderr 摘要；退出 `0` 和用于控制的 `2` 保留原语义 |
| 事件监听器 | 故障 listener 被隔离，并通过 `AgentConfig.on_error` 上报；错误报告 callback 再失败时回退到 stderr |
| 后台任务 | 失败绑定创建任务时的会话；监视或完成通知出错仍保留根因并结束任务状态 |
| MCP | 关联 server、方法、请求与工具调用；保留有界且脱敏的服务 stderr，连接关闭或读线程错误及时结束等待 |
| 会话保存 | 独立日志保留写入失败及原始 cause；会话仍暂停调度并保留未保存的逻辑历史 |
| CLI/TUI | 启动、命令、工作线程及关闭异常留下同样的诊断；CLI 配置错误返回 `2`，运行失败返回 `1`，Ctrl-C 返回 `130` |

工具结果钩子可以恢复错误并返回成功结果。原始失败放在 `ToolResult.details["related_errors"]`，便于分析恢复前发生了什么；成功标志仍按恢复后的结果处理。普通工具主动返回 `ToolResult.error(...)` 时也会得到诊断，但没有原始异常对象的记录不会伪造堆栈。

用户主动取消保持 `aborted`，不伪造 fatal 故障。已失败后在退避等待中取消，失败尝试的诊断仍保留。

### 不完整流

Provider 必须发出 `done` 或 `error` 终止事件。`EventStream` 只看到 EOF 时返回 `STREAM_INCOMPLETE`，保留已接收的 partial 内容。OpenAI Chat 的 `finish_reason`、Anthropic 的 `message_stop` 在适配器内单独检查；缺少这些服务端完成标记会产生可重试的传输错误，不能再默认为成功。

已经开始服务端搜索或网页读取的失败继续遵守原有保护：保留证据并停止自动重放，避免重复执行可能已计费的操作。

## 存储与诊断自身的失败

`DiagnosticLog` 不调用 `SessionStore.append()`，不会递归写入已经损坏的会话。每个实例使用一个独立日志，线程间串行写入，同一错误 ID 去重。文件默认以 `0600`、新目录以 `0700` 创建；Windows 仍遵守系统文件权限模型。

每个日志文件默认上限 5 MiB，保留两份轮转文件；消息、堆栈、异常链、上下文深度及容器数量都有上限，裁剪有明确标记。这个上限针对单个实例的文件族，历史实例文件不会被自动跨实例删除。

创建目录、序列化、写入或关闭失败时，记录包含 `logging_error`，并尝试向 stderr 输出脱敏 JSON。UI 提示出错也走受保护的 stderr 出口。stderr 自身不可写时无法保证外部持久化，但不会用新的报告异常覆盖原始错误或递归触发报告。

## 数据边界

诊断只主动采集排查所需的元数据：不读取 frame locals，不保存源代码行、完整请求 body、请求 headers、system prompt、对话消息或工具参数。敏感键、常见认证文本、实际传入的 Provider token 和凭据环境变量会被脱敏；URL 用户信息、query 和 fragment 不保留。

错误消息和工具/MCP stderr 本身仍可能包含应用生成的内容。扩展应把 `context` 限制为 ID、阶段、方法、计数和状态，不把任意业务对象当诊断字典。诊断脱敏也不改变原有会话正文和工具输出文件的存储契约。

`AssistantMessage.error_details` 和 `ToolResult.details` 是 Harness 元数据，适配器构造模型请求时不发送它们。新字段可被会话 JSONL 恢复；缺少这些字段的旧会话继续正常读取。JSON print 客户端可在助手消息和 `tool_execution_end` 中读取 `error_details`，后者还可能带 `related_errors`。

## 扩展接入

工具可以直接抛异常，由 Agent 捕获。如果需要自行恢复或调整结果，在捕获点生成详情：

```python
from virgo.agent import ToolResult
from virgo.diagnostics import capture_error, safe_text

def execute(args, ctx):
    try:
        return do_work(args)
    except OSError as error:
        details = capture_error(
            error,
            phase="extension.index.read",
            context={"agent": ctx.agent, "tool_call_id": ctx.call_id},
        )
        return ToolResult.error(safe_text(error), details={"error": details})
```

后台回调通过 `session.report_error(...)` 持久化，显式传入排队时捕获的 `store`。如果依赖 SDK 的异常字符串会带入整个响应对象，使用 `capture_error(..., message=<安全错误消息>)`，既保留原异常类型与堆栈，又避免复制完整 body。

`AgentConfig.on_error` 与 `EventBus(on_diagnostic=...)` 允许独立集成方接入自己的诊断出口。完整兼容约定见 [扩展迁移](EXTENSION_MIGRATION.md#结构化错误诊断)。

## 验证

新增离线回归覆盖真实 Session、并行工具和后台任务，以及本机 HTTP/MCP 假服务：异常链、所有工具阶段、重试后成功、原会话归属、权限拒绝、listener/handler 隔离、HTTP request ID、无 request 的网络异常、流截断、凭据脱敏、日志损坏、轮转和 CLI 退出码。

```sh
uv run python ci/run_pytest.py -q tests/test_diagnostics.py tests/agent/test_diagnostics.py tests/ai/test_stream.py tests/ai/providers/test_error_diagnostics.py tests/harness/test_diagnostics.py tests/harness/test_diagnostic_correlation.py tests/harness/test_extension_diagnostics.py tests/cli/test_error_diagnostics.py
uv run python ci/run_pytest.py -q tests/features/test_async_diagnostics.py tests/features/test_mcp_diagnostics.py
```
