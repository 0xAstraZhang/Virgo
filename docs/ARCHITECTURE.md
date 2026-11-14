# Virgo 架构

## 专题设计

- [抽象边界与维护规则](designs/abstraction-boundaries.md)：共享策略、Runtime 身份归属、保留的分层与待收敛接口。
- [web_fetch：原生优先的网页读取](designs/web-fetch.md)：原生能力声明、统一路由与去重、工具契约、权限、回退和端到端测试。
- [会话保存失败处理计划](designs/session-save-failure.md)：失败状态、停止调度、UI 提示和 JSONL 兼容。
- [工具权限显式接口计划](designs/permission-policy-interface.md)：两阶段检查、钩子顺序兼容和异常拦截。
- [Runtime 创建与会话归属计划](designs/runtime-construction.md)：目标存储、记账归属、子任务审查来源和构造期间的扩展上下文。
- [错误诊断](ERRORS.md)：异常采集、模型可见摘要、按 Runtime 取证、追加式投递、MCP 定向恢复与独立日志。

以上三项已在 PR #36 实施。设计文档保留原问题和验收边界；扩展接入方式见 [扩展迁移](EXTENSION_MIGRATION.md#持久化故障权限策略与-runtime-构造)。

保存失败会保留逻辑历史、设置不可自动恢复的 `SessionStore.save_error` 并取消依赖该存储的任务。通知在存储锁外送达；故障文件不再追加，UI 保留未保存提示。权限通过 Harness 的显式策略接口执行两次检查，同时保留扩展优先级和单次派发快照。主、子 Agent 和队友通过 `create_runtime()` 绑定消息、记账与审查来源；候选构造不修改活动 store。

## 分层

```
                ┌──────────────────────────── cli ────────────────────────────┐
                │  Textual TUI / plain REPL           -p print   --mode json │
                └──────────────────────────────┬──────────────────────────────┘
                                               │ prompt / notify / abort
┌───────────────────────────────────── harness ───────────────────────────────────────┐
│ AgentSession ── Runtime(main) ── Runtime(teammate…) ── Runtime(subagent…)            │
│   settings · SessionStore(JSONL 树) · system prompt · EventBus/ExtensionAPI          │
│   commands/templates · compaction · retry/fallback · notify/wakeups                  │
│   features/*（内置扩展）  +  ~/.virgo/extensions  +  .virgo/extensions               │
└──────────────────────────────┬───────────────────────────────────────────────────────┘
                               │ AgentConfig(tools, call_model, hooks)
┌──────────────────────── agent ─────────────────────────┐
│ Agent: messages · steer/follow_up 队列 · abort · 订阅   │
│ run_loop: 模型调用 → 工具（preflight → 并行执行）→ 循环   │
└──────────────────────────────┬─────────────────────────┘
                               │ stream(model, Context, StreamOptions) -> EventStream
┌───────────────────────── ai ───────────────────────────┐
│ types（消息/内容块）· transform（回放规范化）· validate   │
│ providers: anthropic（官方 SDK）· openai · faux          │
└────────────────────────────────────────────────────────┘
```

依赖方向自上而下：`ai` 不知道 agent 的存在，`agent` 不做任何 I/O，所有应用逻辑都在 `harness` 和 `features` 里。

跨调用方共用的规则放在其语义归属层：OpenAI 推理强度映射由 `ai/providers/reasoning.py` 同时服务请求构造和诊断元数据；权限模式与计划模式工具筛选由 `harness/permissions.py` 提供。具体权限策略继续位于 `features/permission.py`。需要确认工具调用归属的功能使用 `AgentSession.runtime_for_context()`，不自行复制身份检查，也不借用当前选中的主 Runtime。

## 终端界面（`virgo.cli.tui`）

`--ui auto` 在 stdin/stdout 均为 TTY 且 `TERM` 可用时选择 Textual 全屏界面，否则继续使用 `ConsoleBroker + TerminalUI`。`--ui plain` 显式选择逐行 REPL；`--ui fullscreen` 在不支持的环境中返回错误。`-p` 和 JSON 事件流使用独立的 `PrintUI`。

- **`VirgoApp`**：管理布局、输入、补全、滚动、权限弹层和会话选择器。对话占剩余高度，底部固定输入和状态栏；正文和背景使用终端默认色。Textual 管理 alternate screen 和终端恢复。
- **`ConversationPane`**：运行期间切换或新建会话时，为另一个会话保留独立的 `AgentSession`、`RunController`、`FullscreenUI` 和界面组件。隐藏的会话继续调度排队命令与唤醒，显示事件暂存至切回后按顺序消费；草稿、滚动、审批、输入表单与补全状态随面板保留。切回运行中的会话复用原运行时，不从磁盘再创建一个写入者。内存会话通过 `memory:<store.id>` 选择，标识不冒充文件路径；它们也显示运行状态，但不能移入回收站。空闲且无需恢复已有面板时沿用原控制器的会话切换流程。退出时先关闭所有桥接与服务，再在统一超时预算内等待所有控制器。
- **`FullscreenUI`**：实现现有 `UI.on_event / print / confirm / wake` 接口。工作线程送来的事件先复制为快照，再放入线程安全队列；`confirm` 在工作线程等待，每个请求独立记录答案与完成信号。
- **`RunController`**：调度同步 `AgentSession` 的工作线程。忙时普通输入走 steering，非并发命令排队，通知触发 `run_pending`。UI 定时消费队列，所有组件更新都发生在界面线程。
- **`Transcript`**：维护消息和工具组件，工具用 agent 与调用 ID 关联。流式更新合并刷新同一条消息，重试清除失败尝试，结束事件校准最终内容；工具和思考可折叠。用户上滚后暂停跟随，回到末尾恢复跟随。

### 运行反馈与详情展开

运行动画属于具体事件：thought、正文生成、工具参数生成分别显示 Thinking、Responding、Preparing；工具执行显示 Running。并发工具和不同 agent 的活动可以同时显示，调用结束只停止对应条目。原生工具按供应商状态及结果关联完成，普通工具按执行开始/结束事件关联；重试、消息结束和整轮结束清理对应活动，历史回放不启动动画。没有具体活动时，在本轮末尾显示临时状态；审批、输入和关闭等待也在这里提示。原有整轮耗时总结继续保留。

`RunActivity` 提供共享动画时钟，`AssistantView` 只更新活跃事件集合中的单行标题。动画不更新正文、不扫描历史，也不请求布局；摘要按内容及宽度缓存，折叠正文在展开时更新。活动属于当前消息中的组件实例，跨 agent 和后续消息复用调用 ID 不会共享动画状态。

tool/thought 标题是不可选中文字的折叠按钮，鼠标点击或 Enter/Space 立即切换，无双击判定等待；详情正文和空白不触发折叠。正文仍支持拖选、双击/三击选择与自动复制。手动上滚后，动画和展开都不主动恢复跟随。

2026-11-13 本机性能抽查（macOS、Python 3.13、Textual 8.2.8）：在 100×35 的 headless 界面挂载 120 条工具记录，每条 26,200 字符，向最后一条标题派发点击，等待展开后的首个刷新回调。修改前约 570 ms，修改后约 40 ms；这是单次本机对比，不代表所有真实终端的延迟。固定双击等待已从标题交互路径移除。

会话恢复读取 `SessionStore.branch()` 的完整当前分支，而不是只读取压缩后的模型上下文。`session_start` 触发视图重建；累计 token 从存储中的助手消息汇总，上下文占用单独估算。

权限请求按顺序显示。中止时释放对应的待决确认，工具预检在权限检查前检测取消信号。退出时禁止新任务调度并释放全部确认，关闭钩子在后台执行且只执行一次；后台任务、调度器、队友和 MCP 先停止，可选的自动记忆提取最后执行。界面线程不等待工作线程或关闭钩子。

## 命令解释器

`ShellSelection` 保存会话级选择，`ShellTool` 绑定不可变的解释器信息；前台、后台和模型提示使用同一个绑定。`/shell` 在运行边界更新主 Runtime，新子 Runtime 继承当前选择，已有子 Runtime 和后台命令保持原选择。PowerShell 使用独立权限分支。探测、配置优先级、兼容模式和验证入口见 [Shell 选择与执行](SHELLS.md)。

## 消息模型（`virgo.ai.types`）

- 内容块：`TextContent`、`ThinkingContent(signature, redacted)`、`ImageContent`、`ToolCall`、`RawContent`（provider 专有的块，原样回传，例如 Anthropic 的 `fallback`）。
- 消息：
  - `UserMessage(content, kind)`：`kind` 是内部标签，不发给模型，取值有 `summary`、`notification`、`continue`；
  - `AssistantMessage(content, api, provider, model, usage, stop_reason, error_message, error_kind, error_details)`；
  - `ToolResultMessage(tool_call_id, tool_name, content, details, is_error)`。
- `stop_reason` 取值：`stop | length | tool_use | refusal | error | aborted`。
- `error_kind` 取值：`retryable | overflow | fatal`，由适配器分类，harness 据此决定是重试、压缩还是放弃。
- 流事件：`start`、`text_*`、`thinking_*`、`toolcall_*`、`retry`、`done`、`error`。`EventStream` 的约定是迭代过程永不抛出异常。

`virgo.diagnostics` 只采集可序列化、脱敏的错误数据，不依赖 Harness 或写文件。AI 适配器在异常发生处保留原始堆栈和安全 HTTP 元数据；Agent 将工具错误放入 `ToolResult.details["error"]`，用 `AgentConfig.on_error` 上报监听器故障。Harness 的 `DiagnosticLog` 负责有界 JSONL 写入，`AgentSession.report_error()` 负责绑定会话、去重和 UI 提示。诊断文件独立于 `SessionStore`，保存错误不会递归向故障会话追加记录。

`details` 本身仍不发送给 Provider。`AgentConfig.project_tool_result` 是可选的纯数据回调：在工具最终结果入历史前生成模型视图。Harness 默认实现只在最终失败时，向原文本和图片追加最多 2 KiB 的白名单诊断摘要；成功、被结果钩子修复的结果和取消不会因此被改成新故障。回调异常保留原结果与控制标志，不能因诊断视图失败重复执行工具。

流在缺少终止事件时不再被补成成功；保留 partial 并产生明确错误。OpenAI Chat 和 Anthropic 适配器分别检查 `finish_reason`、`message_stop`，不完整传输按现有重试策略处理；已开始原生收费操作的失败仍保留证据并停止自动重放。

## Agent 循环（`virgo.agent.loop`）

```
pending = prompts
while True:
    while need_call or pending:
        追加 pending；msg = call_model(messages)            # 由 harness 实现
        if msg.stop_reason in (error, aborted, refusal): return
        results = execute(msg.tool_calls)                  # stop_reason=length 时全部报错，不执行
        need_call = bool(tool_calls); pending = steering()
    pending = follow_ups();  if pending: continue
    if should_stop() -> Continue(text): pending = [text]; continue
    return
```

工具执行分三步：

1. **preflight**，按源顺序逐个进行：`prepare_arguments` → JSON-Schema 校验与宽松强转 → `before_tool_call`（可以 Block，也可以就地改参）。
2. **执行**：每项执行前调用 `before_tool_execute`，复核预检后改变的权限模式、参数和路径边界；用线程池并行，只要批次里有任一工具声明了 `sequential`，整批改为串行。
3. **收尾**：保留实际 `execution` 与适配器 `transport` 事实，调用 `after_tool_call`；随后应用可选结果投影，再按源顺序追加 `ToolResultMessage`。

未知工具、参数非法、被拦截、执行抛异常，都会变成 `is_error=True` 的结果。

每批工具解析使用同一个工具表快照。MCP 等服务在安全边界刷新工具后，仅下一批使用新表，正在执行的调用继续使用原绑定。`execution` 区分未开始、实际返回和结果钩子失败；MCP `transport` 另外描述发送及响应状态。`read_only` 不用于推断可重放性，超时或结果处理失败也不能把此前副作用标记为不存在。

## 集成运行时（`virgo.harness.session`）

每个对话（主 agent、队友、子 agent）都是一个 `Runtime`：它持有消息、会话存储、system prompt、工具集和模型。`Runtime.call_model` 的处理顺序如下：

1. 在完成工具批次后的安全边界调用 `services["mcp_refresh"](runtime)`；schema 改变时更新本 Runtime 工具并重建 system。显式受限的工具集保留其能力边界和 Runtime 专属工具。
2. 核对原生上下文绑定并检查自动压缩条件。
3. 将该 Runtime 的待处理诊断和压缩后引用追加为通知，再由 `context` 钩子生成本次请求视图。
4. 按当前权限模式发起流式请求：plan 附加只读规划提示、过滤有副作用的工具，并限制 bash 只读；配置与 schema 未变时保持 system prompt 和 tools 稳定。
5. 处理失败：
   - `retryable`：指数退避加抖动，尊重有效 `Retry-After` 的最短等待；`retry.maxWaitSeconds` 默认 120，限制本次逻辑调用的累计重试等待，下一次等待超预算则停止。连续 2 次过载且配置了 `fallbackModel` 时改用回退模型；
   - `overflow`：压缩一次后重试；
   - 其他错误：交还给循环。

每次工具调用依次经过：

1. Harness 在原优先级 100 的位置显式调用权限预检，再继续派发命令钩子（50）和默认用户扩展（0）；任意优先级及同优先级注册顺序保持不变；
2. 执行前复核权限快照，再执行工具；审批使用当前 Runtime 的取消信号；
3. `tool_result` 钩子；
4. 输出预算：超过 50KB 的文本截断并落盘。

agent 准备停止时，依次检查：

1. follow-up 队列（定时任务送达的 prompt）；
2. 用户启用的 `agent_stop` 钩子：命令 Stop 钩子（优先级 20）、goal 评审（0）。Todo 仅记录状态，不注册停止钩子。

**唤醒机制**：后台任务和队友通过 `session.notify(text, mode, store=owner, diagnostics=...)` 绑定原主 Runtime。定时任务沿用既有任务归属路由，再调用 `session.notify(text, mode)`；省略 `store` 时仍面向该 Session 当前的主 Runtime。

- 主 agent 正在运行：按 mode 进入 steer 队列（工具批次后插入）或 follow-up 队列（即将停止时插入）。
- 主 agent 空闲：放入 `wakeups` 并调用 `ui.wake()`，REPL 随即开始新的一轮。

运行结束时，队列里剩下的消息会被转进 `wakeups`，不会丢失。

切换后的原 Runtime 持有自己的待处理队列，后台通知不会唤醒另一个当前聊天。诊断证据先通过 `retain_diagnostics` 留在原 Runtime，再尝试普通完成通知；通知失败时等待原 Runtime 的下一次正常模型请求，不额外启动恢复轮次。

### 诊断访问与投递

| 模块 | 职责与边界 |
|---|---|
| `harness/diagnostic_view.py` | 纯数据白名单投影；工具失败摘要最多 2048 UTF-8 字节，保留错误 ID、观测原因、执行状态和取证入口 |
| `harness/diagnostic_access.py` | 每 Runtime 明确授权的证据副本；默认 100 条记录、1000 个 ID；`summary/causes/trace/stderr/attempts/related` 查询的完整 JSON 页最多 8192 字节 |
| `features/diagnostics.py` | 只读 `diagnostic_get`；校验真实调用 Runtime、Session 和审查来源；不接受文件路径，不查询全局最近错误 |
| `harness/recovery.py` | 授权、待投递、已展示和压缩检查点；从所属分支恢复记录，在自然请求边界追加通知，不决定任务继续或重放 |
| `harness/retry.py` | 解析秒数或 HTTP 日期形式的 `Retry-After`；重试仍由 `Runtime._attempts` 唯一控制 |

共享记账存储不授予子 Agent、兄弟 Runtime 或当前选中聊天额外的诊断访问。完整或截断的工具文本中出现一个错误 ID 也不构成授权；原结果元数据、所属分支记录和显式父子传播才建立引用。过期、缺失和未采集的证据分别返回明确状态，查询失败不递归产生新的诊断。

用户取消和 `SessionStore.save_error` 保持现有调度保护；记录或投递证据不解除这些状态。日志持久化与模型诊断投递相互独立，诊断路径自身故障不能覆盖原操作结果。详细字段和行为见 [错误诊断](ERRORS.md#模型可见的证据)。

### MCP 连接恢复

`MCPConnections` 为每个服务保存稳定派发句柄、递增 generation、进行中调用数、恢复锁和无进展尝试计数。`mcp_status` 只读本地状态；`mcp_reconnect` 依据当前 Runtime 获得的服务错误执行普通受权限约束的恢复。进行中调用或并发恢复会被明确拒绝；候选 initialize、分页 tools/list、schema 和工具包装器全部准备好后才可能发布。工具列表检查有单次握手 deadline、重复 cursor 检查和页数上限。

schema 未变时原工具对象改用新 client。schema 改变时将候选留到 `mcp_refresh`，再次检查注册冲突后替换该服务的注册项；失败保留原 client 和工具，关闭候选并记录原因。旧 Runtime 持有的不同 schema 包装器拒绝派发，避免用旧参数契约操作新服务。该机制不重读模型工具的启动命令；人工 `/mcp reconnect` 仍可重新加载配置。

无远端工具调用成功时最多两次恢复尝试，握手成功本身不重置预算。每次重连都不重放旧 `tools/call`；`sending`、`sent` 后的失败保留 `outcome=unknown` 和 `side_effects=unknown`。连接恢复、业务操作结果核对与模型的下一次工具选择是不同步骤，没有通用的“恰好执行一次”保证。

## 提示与模型决策

基础提示只提供身份、目标、按任务需要验证和如实报告的要求，并说明工具结果与通知不授予新的用户授权。工具描述仅在实际工具定义中出现；`AgentTool` 不再提供 `snippet`。实际 shell 环境、用户系统文件、项目指令及非空资源目录继续注入。技能可按需选用、使用前读取；显式 `/skill:` 仍展开用户指定的技能。记忆为空时不注入段落，有内容时只注入索引与读取入口。用户显式配置的 `memory.autoExtract` 仍保留专用提取流程。

默认主 Agent 提供 28 个工具（原 25 个加 `diagnostic_get`、`mcp_status`、`mcp_reconnect`），角色能力和 `features.*` 配置继续生效。该数量使用空项目、默认功能及客户端网页工具；MCP 服务、用户扩展和原生网页路由会改变实际工具集。工具校验、执行前权限复核、依赖与锁、取消、重试、输出预算以及追加式历史继续由 Harness 保障。权限评审仍区分风险与授权，只有真实用户消息和记录的人工批准可提供显式授权，后续限制可撤回先前授权；Goal 只评审用户指定条件与现有证据，不附加统一验收清单。

Todo 支持并行进行项及整表替换，不驱动下一次模型调用。团队完成当前委派后等待消息，不自动领取持久化任务；`task_update(status="in_progress")` 显式认领，`team_message` 委派或唤醒，仅修改 owner 不唤醒。退出仍释放该队友未完成的进行中任务。

`ask_user` 的超时、取消和非交互结果均不包含 `choice` / `answer`；超时仍撤回表单并记录 `answered=false, timed_out=true`。用户 steering 会撤销已显示问题的超时，实际作答会取消后续问题的无人值守时限。这些结果不决定模型下一步如何工作。

用户主动启用的 `/goal`、Workflow、Stop hook 和 `plan_first` 保留。子 Agent 仍不递归、默认最多 50 轮，`pause_turn` 仍最多续接三次；本轮未改变这些执行边界。接口迁移见 [扩展迁移说明](EXTENSION_MIGRATION.md)。

## 只追加的请求

在同一个会话内，模型配置、权限模式、shell 和工具 schema 未变且两次压缩之间：

- `system` 和 `tools` 逐字节不变；
- 每次请求的 `messages` 都是上一次请求的前缀加上新增内容。

适配器合并相邻同角色消息的方式是确定性的，所以合并结果每次相同。这样提示缓存可以命中，Claude 的 thinking 块也能保持有效。

`tests/harness/test_session.py::test_requests_are_append_only` 会验证这一点。

失败摘要在新工具结果入历史时只投影一次，诊断通知和压缩后的引用以新消息追加。不得在每次 `request_context` 里扫描旧错误并重写内容。MCP schema 切换会明确更新工具前缀，并继续按原生压缩的配置指纹处理兼容性。

每次模型调用前检查压缩条件，所有 Provider 默认阈值为模型上下文窗口的 80%（`compaction.triggerRatio=0.8`）；显式 `triggerTokens` 优先，旧 `reserveTokens` 不再使用。`/compact` 可手动触发，overflow 时另尝试一次压缩。

- OpenAI API 使用 `/responses/compact`，把完整返回窗口保存为 `RawContent`，后续经 Responses API 原样回传；原 Chat Completions 连接在压缩后自动改用 Responses。Codex 账号使用普通 Responses 请求附加 `compaction_trigger`，保留用户输入和返回的唯一压缩项。
- Anthropic 使用当前模型的按需压缩（`compact-2026-09-04`、`compaction.type=summarize`），仅提交较早消息，保存完整明文摘要和签名；后续请求继续携带 beta header。
- 其他 Provider 使用小模型续接摘要，保留原始目标、后续修正、约束、进展、未解决事项和关键事实，结构由模型选择；支持旧摘要增量更新、文件列表和重点提示。第三方仅兼容 OpenAI/Anthropic 协议不会自动启用原生压缩。
- Anthropic 和通用摘要按 `keepRecentTokens` 选择切点，工具调用与结果保持在同侧；必要时补回最新用户请求。OpenAI 的完整返回窗口不再自行裁剪。
- 通用摘要先完整序列化已有的用户、助手及工具文本，不对单次参数或结果预裁剪；超过 600,000 字符总预算时保留首尾并插入遗漏标记，标记计入预算。这里不会重新读取已被工具输出预算截断的原始文件。
- 通用摘要剥离近期 thinking 并重建 system；原生压缩不重建 system。Anthropic 在连续保留、角色边界合适且模型/system/tools 配置不变时保留近期 thinking；补回请求或前缀配置改变时剥离。配置指纹随压缩记录保存，恢复后也会核对。
- 向会话文件追加 `compaction` 条目，原始消息不删除；原生压缩失败、取消或返回不完整结果时不替换历史。
- 原生状态不跨 Provider 发送；切换到不兼容 Provider 时恢复当前分支完整消息，并重新估算上下文。摘要调用和被保留消息的旧 usage 不用于压缩后的计数。

诊断授权和投递记录属于原会话分支，不依赖压缩摘要是否提到故障。压缩后的第一次自然请求可追加一次近期诊断引用检查点；其作用是保留取证入口，既不声明故障仍未恢复，也不触发重试或新的模型轮次。证据本身仍受 Runtime 的保留上限约束。

## 同进程的定时任务归属

同一进程内按解析后的 `.virgo/schedules.json` 路径共享任务状态、ID 分配和锁。
持久化任务仅由一个 Scheduler 投递：文件加载时归首个会话，新建时归创建它的会话。
其他同工作空间会话可以列出和取消持久化任务；添加或取消不会覆盖另一会话的任务。
非持久化任务仅对创建会话可见，并随该会话关闭而清理。拥有者关闭时，尚未触发的
持久化任务交给同工作空间的另一存活会话；最后一个会话关闭后释放共享状态，下次
启动重新读取文件。单次任务删除、cron 的 `last_fired` 更新和保存都在共享锁内完成。
此协调范围是一个 Virgo 进程，不是多个独立启动的 Virgo 进程。

## 会话文件

文件位置：`~/.virgo/sessions/<cwd-slug>/<时间>_<id>.jsonl`

```
{"type":"session","version":1,"id":"…","cwd":"…","timestamp":"…"}
{"type":"message","id":"a1b2c3d4","parent_id":null,"timestamp":"…","message":{…}}
{"type":"compaction","id":"…","parent_id":"…","summary":"…","kept_ids":["…"],"tokens_before":123456,
 "details":{"read_files":[…],"modified_files":[…]}}
{"type":"custom","id":"…","parent_id":"…","kind":"goal","data":{…}}
{"type":"model_change","id":"…","parent_id":"…","model":"openai/gpt-5"}
```

`build_context()` 从叶子节点回溯到根，得到当前分支：

- 分支上没有 compaction 条目时，就是原始的消息列表；
- 有 compaction 条目时，依次输出：原生 `context`（没有此字段的旧记录使用 `summary`）、`kept_ids` 指向的消息、compaction 之后的所有消息。`preserve_thinking` 控制保留消息是否剥离 thinking，`context_binding` 记录原生压缩时的模型/system/tools 指纹。恢复时清除压缩前 usage，压缩后的新消息 usage 保留。

队友的会话存放在 `<会话 id>-team/<name>.jsonl`。

同一 AgentSession 切走的文件会话保留原 Runtime 和输入队列，避免迟到的后台保存故障被磁盘重载绕过。会话列表包含仍在内存中的故障历史；重新打开优先复用该实例，并同步当前模型选择。TUI 保留故障会话所在的面板。恢复边界与缓存生命周期见[保存失败处理](designs/session-save-failure.md#切换后的恢复)。

## 项目状态目录 `.virgo/`

| 路径 | 内容 |
|---|---|
| `settings.json`、`mcp.json` | 项目配置 |
| `skills/`、`agents/`、`prompts/`、`workflows/`、`extensions/` | 项目资源 |
| `memory/*.md`、`memory/MEMORY.md` | 项目级记忆 |
| `tasks/<id>.json`、`tasks/.lock` | 任务图 |
| `schedules.json` | 持久化的定时任务 |
| `worktrees/<name>` | 队友的 git worktree |
| `runs/<run_id>.jsonl` | workflow journal |
| `outputs/` | 被截断的工具输出全文、后台任务日志 |

## 扩展 API 速查

```python
api.on(event, fn, priority=0)
api.register_tool(AgentTool(...), roles=("main",))   # 可用角色: main / subagent / teammate
api.register_command(name, fn, description, concurrent=False, *, argument_hint="")
api.add_system_section(name, fn(role) -> str | None, order=50)
api.notify(text, mode="steer" | "follow_up", store=original_store, diagnostics=[record])
api.append_entry(kind, data); api.entries(kind)
api.services        # 功能之间共享的服务: permissions, jobs, tasks, team, subagents, memory, skills, mcp, schedule, goal
api.session / api.settings / api.paths / api.ui
```

| 事件 | 处理函数签名 | 返回值的作用 |
|---|---|---|
| `session_start` / `session_shutdown` | `(session)` | — |
| `input` | `(text, session)` | 返回 `str` 替换输入；返回 `Handled(msg)` 表示已处理 |
| `before_agent_start` / `agent_end` | `(runtime, prompts)` / `(runtime)` | — |
| `agent_event` | `(AgentEvent, runtime)` | — |
| `context` | `(messages, runtime)` | 返回请求视图（必须保持只追加） |
| `tool_call` | `(ToolCallEvent, session)` | 返回 `Block(reason)` 拦截；可以就地修改 `ev.args` |
| `tool_result` | `(ToolResult, ToolCallEvent, session)` | 返回新的 `ToolResult` |
| `turn_end` | `(AgentEvent, runtime)` | — |
| `agent_stop` | `(runtime)` | 返回 `Continue(text)` 让 agent 继续 |
| `before_compact` / `compact` | `(runtime)` / `(runtime, summary)` | — |
| `pending_work` | `(session)` | 返回真值表示还有未完成的工作（print 模式会等待） |

命令参数提示的兼容性、解析优先级及渲染规则见 [ARGUMENT_HINTS.md](ARGUMENT_HINTS.md)。
