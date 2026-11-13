# 错误诊断

发生故障时，应能回答：哪次运行、哪个 Agent、哪个阶段失败，根因是什么，是否重试过，以及去哪里读取证据。Virgo 为这些问题提供统一错误记录，并把诊断写入独立于会话历史的日志。

## 如何排查

1. 记录界面显示的错误 ID。错误提示包含阶段、异常类型、简短原因和 `Diagnostics:` 路径。
2. 在当前运行实例输入 `/diagnostics` 查看最近十条错误和日志路径；输入 `/diagnostics <error-id>` 查看完整 JSON。支持唯一的 ID 前缀；查询范围为当前实例保留的最近一百条记录。
3. 重启后直接读取提示中的 JSONL 文件；如果已轮转，同时检查同名 `.1`、`.2` 文件。每行是一条独立 JSON 记录，可按 `error_id`、`context.run_id` 或 `context.tool_call_id` 检索。
4. 模型失败先看 `kind` 与 `context.http`，本地失败先看 `phase`、`exception_type`、`cause_chain` 和 `traceback`。重试失败再成功的记录仍在日志中。

日志位于 `~/.virgo/logs/harness-<UTC时间>-<进程号>-<随机ID>.jsonl`。设置 `VIRGO_HOME` 后使用其 `logs/` 子目录。日志在首次错误时创建，不需要 debug 开关。多个全屏会话实例各有日志；同一实例执行 `/new` 后继续使用原日志，以每条记录的 `session_id` 区分归属。

`--no-session` 只关闭对话历史持久化，故障仍会生成诊断日志。独立日志避免保存会话失败时丢失最后的故障原因。

## 模型可见的证据

### 失败摘要与按需查询

工具的最终结果仍失败时，Agent 在结果钩子结束后、写入 `ToolResultMessage` 前追加一块最多 **2048 UTF-8 字节（2 KiB）**的诊断摘要。原有文本、图片、工具调用 ID 和控制标志保留；这个预算只限制新增摘要，不是整个工具输出的大小。摘要选择错误 ID、阶段、代码、观测到的异常及较深层 cause、执行/传输事实和查询入口，超出预算时标记省略。它不把异常消息中的建议当作授权，也不把观测事实直接认定为唯一根因。

模型按需调用 `diagnostic_get(error_id, section="summary", cursor=None)`。这是普通只读工具，参数不接受日志路径或任意文件路径，也不支持错误 ID 前缀。人工 `/diagnostics` 面向当前实例日志；模型查询仅使用调用 Runtime 自己已获得的证据，两者的范围不同。

模型诊断默认启用。`features.diagnostics=false` 关闭 `diagnostic_get`、结果诊断摘要和自动诊断通知，不关闭独立日志。若 Runtime 的显式工具名单未包含 `diagnostic_get`，摘要的 `evidence` 仍保留错误 ID，并标记 `availability="query_tool_not_exposed"`，不会宣称该工具可调用或扩展原工具权限。

| `section` | 返回的证据 |
|---|---|
| `summary` | 白名单错误字段、上下文和异常链数量 |
| `causes` | 已保留的异常类型、消息、cause/context 关系及有界堆栈位置 |
| `trace` | 已采集的堆栈文本 |
| `stderr` | 已采集、脱敏且有界的 MCP stderr；不是任意进程输出文件读取入口 |
| `attempts` | 同一 Runtime、会话、运行、逻辑模型调用和用途下，当前仍保留且已授权的失败记录；不代表全部尝试或成功记录 |
| `related` | 已显式传播且仍获授权的关联诊断；一个引用本身不会授予读取权限 |

每个 Runtime 默认保留 **100 条证据、1000 个已授权 ID**。证据逐出后，在 ID 仍被记住时返回 `expired`；未知或不属于该 Runtime 的 ID 均返回 `not_found`，不会尝试全局日志、相似 ID 或其他会话。未采集某类证据返回 `unavailable`。共享 `accounting_store`、同名 Agent、UI 当前选中的会话都不会扩大权限；父 Runtime 仅在收到明确传播的子任务诊断后获得对应 ID。

一次查询的完整 JSON 响应最多 **8192 UTF-8 字节（8 KiB）**。正常响应包含 `text`、`next_cursor` 和 `source_truncated`；继续查询时保持同一个 `error_id` 和 `section`，按顺序拼接 `text`。游标绑定错误、栏目及证据快照，跨范围、过期快照或损坏的游标返回 `invalid_cursor`，需要不带游标重新开始。缺失、过期和查询失败属于明确的查询结果，不递归制造新的工具故障。

例如，模型已收到某个错误 ID 后，可依次读取：

```text
diagnostic_get(error_id="<已收到的ID>", section="summary")
diagnostic_get(error_id="<同一个ID>", section="stderr")
```

### 执行状态与结果未知

结果使用字典 `details` 时，`ToolResult.details.execution` 记录本地工具边界，`transport` 记录 MCP 请求边界。Agent 在结果钩子前保留这些事实，钩子不能把已开始的操作改成“未执行”，也不能通过替换字典结果移除原始传输状态。正常成功结果若使用非字典 `details`，仍保留扩展原有载荷类型，不为追加执行元数据改成字典。

| 字段 | 含义 |
|---|---|
| `execution.started` | 是否已进入工具执行函数；参数校验、权限阻止发生在它之前 |
| `execution.outcome` | `not_started`、`returned_success`、`returned_error` 或 `unknown` |
| `execution.result_hook` | `not_run`、`returned` 或 `failed`，独立表示结果钩子的处理 |
| `transport.dispatch` | `not_started`、`sending`、`sent` 或 `response_received` |
| `transport.outcome` | 远端响应或传输的已知结果；丢失响应、发送异常不能宣称未执行 |
| `side_effects` | 仅在确认未执行/未派发时使用 `none`；其余保留 `unknown`，不从 `read_only` 推断幂等性 |

如果工具已经返回成功，但结果钩子失败，实际执行成功的事实仍在；不能因最终 `is_error=true` 就重放原操作。Shell 非零退出、超时及子 Agent 最终失败也可能已经留下文件修改或外部副作用。子 Agent 错误正文会保留近期工具结果及可能已生效的提醒，不能据此推定整个子任务可安全从头重跑。

### Runtime 投递与历史

每个 Runtime 持有自己的诊断访问集合和投递记录。工具摘要只在新结果入历史时构造一次；后续请求不重新投影或改写旧结果。运行时故障、启动故障和显式传回的异步证据，在下一次本来就要发出的模型请求前作为通知追加。记录诊断、查询失败或发现待处理证据都不会单独创建模型轮次。

会话 `custom` 记录保存诊断授权、待投递和已展示状态；恢复仅从所属分支的消息及这些记录重建，不扫描全局日志来扩大范围。压缩后在自然请求边界追加一次简短诊断引用检查点，具体证据仍受保留预算约束。检查点可能指向已经恢复的旧故障，只是查询入口，不是重新执行或判定任务尚未完成的指令。

后台任务和队友通知绑定创建时的原会话。完成通知失败时，故障证据仍留在原 Runtime，等待其下一次正常请求；不会唤醒另一个刚被选中的聊天。会话成功移入回收站后，迟到的后台通知与待投递证据不再交给该 Runtime，也不转投当前聊天。用户取消、`SessionStore.save_error` 和现有调度停止检查仍优先，诊断投递不能解除取消或续写故障存储。诊断处理自身失败使用受保护的 stderr 出口，不覆盖原结果。

## 有界恢复

### MCP 定向重连

`mcp_status(server?)` 读取本地连接状态、在役 `generation`、进行中的调用数量、剩余恢复次数及当前 Runtime 可访问的错误 ID，不向 MCP 服务发请求。`mcp_reconnect(server, error_id)` 是有副作用的普通工具，继续走权限预检和执行前复核；`plan` 不允许它。调用源必须是真实、属于本 AgentSession 的 Runtime，来自其他会话的上下文或错误 ID 不能控制此处连接。同一会话内切换 UI 后，原 Runtime 的合法调用仍保持原归属。

重连要求诊断属于指定服务及其在役 generation。每个服务同时只能有一个恢复候选；存在进行中的调用、已排队候选或旧 generation 的诊断时明确拒绝，不隐式中断或重放那些调用。候选使用原注册配置和原工作目录，模型工具不会修改配置，也不会重读新的 `command`。人工 `/mcp reconnect` 保留全服务配置重载行为，同样不能打断进行中的调用。

候选必须完成 `initialize`、`tools/list` 和 Virgo 实际使用字段的 schema 检查。握手和分页共用配置中的 `timeout`（默认 30 秒），重复分页游标被拒绝，工具列表最多读取 100 页。工具包装器与名称冲突检查在发布前完成；候选无效时保留原连接及原工具，并记录失败证据。

schema 未变时只替换连接句柄并递增 generation。schema 变化或启动时未注册工具时，先返回 `refresh_required`，在工具批次结束后的模型请求边界发布工具列表。发布前再次检查冲突，失败会关闭候选并保留原注册表；旧 Runtime 的不兼容 schema 包装器拒绝派发。显式限制过工具列表的子 Runtime 不会因此获得额外能力。

每个服务连续没有远端工具调用成功时，最多允许 **2 次**恢复尝试；握手成功、generation 增长都不会单独清空预算，成功的远端工具调用才代表这里的连接进展。拒绝进行中的调用不消耗尝试预算。耗尽后返回明确结果，由模型基于新证据调整方案或请求用户处理。

重连成功只确认连接恢复，`replayed_calls` 为 `0`。特别是 `sending`/`sent` 后失败或回复丢失时，远端操作可能已经执行；下一次修改性操作前必须先核对状态。当前实现不提供通用幂等键、业务操作回滚或“恰好执行一次”的保证。

### Provider 重试

Provider 恢复继续由 `Runtime._attempts` 统一控制，不引入第二层诊断重试循环。`retry.maxRetries` 默认 3；`retryable` 使用指数退避与抖动，有效的 HTTP `Retry-After` 秒数或 HTTP 日期是最短等待，非法值忽略。

`retry.maxWaitSeconds` 默认 **120 秒**，是一次逻辑模型调用内重试等待的累计预算，不是整个请求或任务的超时。若下一次应等待的时间超过剩余预算，保留当前失败并停止，不把 `Retry-After` 截短后提前请求。取消可中断等待。已有的单次 overflow 压缩、配置的过载回退模型，以及原生收费操作开始后不自动重放的边界保持不变。

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
| 模型请求与重试 | 每次失败记录真实模型、实际请求 API、调用 ID、尝试序号和耗时；API 优先取失败消息，缺失时才回退配置值，原生网页工具或压缩上下文切换到 Responses 后也如此。成功不会删除前次失败，原有退避、回退模型及 overflow 压缩策略继续决定恢复行为 |
| 模型目录 | 在认证后的请求边界采集 `catalog.fetch` 异常，使用本次实际 key 和自定义认证 header 值对整条异常链脱敏；后台刷新、日志与 stderr 复用同一份诊断 |
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

`AssistantMessage.error_details` 和 `ToolResult.details` 保持 Harness 元数据契约，适配器不会自动序列化它们。模型只收到显式白名单投影后的摘要和 `diagnostic_get` 返回的有界证据。新字段可被会话 JSONL 恢复；缺少这些字段的旧会话继续正常读取。JSON print 客户端可在助手消息和 `tool_execution_end` 中读取 `error_details`，后者还可能带 `related_errors`、`execution` 和 `transport`。

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

需要让特定 Runtime 后续读取证据时，使用该 Runtime 的 `report_error()`，或在实际交付结果时显式传播 `details["error"]` / `related_errors`。后台通知可传 `api.notify(..., store=original_store, diagnostics=[record])`；应在可能失败的通知之前调用 `session.retain_diagnostics(original_store, [record])`，保留原会话的待处理证据。不要用全局最近错误、共享记账存储或错误文本中的 ID 来授予查询范围。

`AgentConfig.on_error` 与 `EventBus(on_diagnostic=...)` 允许独立集成方接入自己的诊断出口。完整兼容约定见 [扩展迁移](EXTENSION_MIGRATION.md#结构化错误诊断)。

## 验证

新增离线回归覆盖真实 Session、并行工具和后台任务，以及本机 HTTP/MCP 假服务：异常链、所有工具阶段、重试后成功、原会话归属、权限拒绝、listener/handler 隔离、HTTP request ID、无 request 的网络异常、流截断、凭据脱敏、日志损坏、轮转和 CLI 退出码。

```sh
uv run python ci/run_pytest.py -q tests/test_diagnostics.py tests/agent/test_diagnostics.py tests/ai/test_stream.py tests/ai/providers/test_error_diagnostics.py tests/harness/test_diagnostics.py tests/harness/test_diagnostic_correlation.py tests/harness/test_extension_diagnostics.py tests/cli/test_error_diagnostics.py
uv run python ci/run_pytest.py -q tests/features/test_async_diagnostics.py tests/features/test_mcp_diagnostics.py
uv run python ci/run_pytest.py -q tests/agent/test_diagnostic_projection.py tests/harness/test_diagnostic_view.py tests/features/test_diagnostic_access.py tests/harness/test_recovery_lifecycle.py tests/features/test_mcp_recovery.py
```

模型可见性测试检查真正发给离线 Provider 的请求、追加式消息和恢复后的授权范围；MCP 测试启动真实本地子进程，包含“服务已写入一次副作用后丢失回复，重连不再写入”的场景。这些测试验证证据、调度和重放边界，不衡量真实模型的根因判断质量或线上自主恢复率。
