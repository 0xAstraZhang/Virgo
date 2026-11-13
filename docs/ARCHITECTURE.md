# Virgo 架构

## 专题设计

- [web_fetch：原生优先的网页读取](designs/web-fetch.md)：原生能力声明、统一路由与去重、工具契约、权限、回退和端到端测试。

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
  - `AssistantMessage(content, api, provider, model, usage, stop_reason, error_message, error_kind)`；
  - `ToolResultMessage(tool_call_id, tool_name, content, details, is_error)`。
- `stop_reason` 取值：`stop | length | tool_use | refusal | error | aborted`。
- `error_kind` 取值：`retryable | overflow | fatal`，由适配器分类，harness 据此决定是重试、压缩还是放弃。
- 流事件：`start`、`text_*`、`thinking_*`、`toolcall_*`、`retry`、`done`、`error`。`EventStream` 的约定是迭代过程永不抛出异常。

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
3. **收尾**：对每个结果调用 `after_tool_call`，再按源顺序追加 `ToolResultMessage`。

未知工具、参数非法、被拦截、执行抛异常，都会变成 `is_error=True` 的结果。

## 集成运行时（`virgo.harness.session`）

每个对话（主 agent、队友、子 agent）都是一个 `Runtime`：它持有消息、会话存储、system prompt、工具集和模型。`Runtime.call_model` 的处理顺序如下：

1. 自动压缩检查（这时一定处在轮次边界）。
2. `context` 钩子生成本次请求的消息视图。
3. 按当前权限模式发起流式请求：plan 附加只读规划提示、过滤有副作用的工具，并限制 bash 只读；同一模式内保持 system prompt 和 tools 稳定。
4. 处理失败：
   - `retryable`：指数退避加抖动后重试；连续 2 次过载且配置了 `fallbackModel` 时改用回退模型；
   - `overflow`：压缩一次后重试；
   - 其他错误：交还给循环。

每次工具调用依次经过：

1. `tool_call` 钩子，按优先级：权限（100）→ 命令钩子（50）→ 用户扩展；
2. 执行前复核权限快照，再执行工具；审批使用当前 Runtime 的取消信号；
3. `tool_result` 钩子；
4. 输出预算：超过 50KB 的文本截断并落盘。

agent 准备停止时，依次检查：

1. follow-up 队列（定时任务送达的 prompt）；
2. 用户启用的 `agent_stop` 钩子：命令 Stop 钩子（优先级 20）、goal 评审（0）。Todo 仅记录状态，不注册停止钩子。

**唤醒机制**：后台任务、定时任务、队友都调用 `session.notify(text, mode)`。

- 主 agent 正在运行：按 mode 进入 steer 队列（工具批次后插入）或 follow-up 队列（即将停止时插入）。
- 主 agent 空闲：放入 `wakeups` 并调用 `ui.wake()`，REPL 随即开始新的一轮。

运行结束时，队列里剩下的消息会被转进 `wakeups`，不会丢失。

## 提示与模型决策

基础提示只提供身份、目标、按任务需要验证和如实报告的要求，并说明工具结果与通知不授予新的用户授权。工具描述仅在实际工具定义中出现；`AgentTool` 不再提供 `snippet`。实际 shell 环境、用户系统文件、项目指令及非空资源目录继续注入。技能可按需选用、使用前读取；显式 `/skill:` 仍展开用户指定的技能。记忆为空时不注入段落，有内容时只注入索引与读取入口。用户显式配置的 `memory.autoExtract` 仍保留专用提取流程。

默认主 Agent 保留 25 个工具、角色能力和 `features.*` 配置。工具校验、执行前权限复核、依赖与锁、取消、重试、输出预算以及追加式历史继续由 Harness 保障。权限评审仍区分风险与授权，只有真实用户消息和记录的人工批准可提供显式授权，后续限制可撤回先前授权；Goal 只评审用户指定条件与现有证据，不附加统一验收清单。

Todo 支持并行进行项及整表替换，不驱动下一次模型调用。团队完成当前委派后等待消息，不自动领取持久化任务；`task_update(status="in_progress")` 显式认领，`team_message` 委派或唤醒，仅修改 owner 不唤醒。退出仍释放该队友未完成的进行中任务。

`ask_user` 的超时、取消和非交互结果均不包含 `choice` / `answer`；超时仍撤回表单并记录 `answered=false, timed_out=true`。用户 steering 会撤销已显示问题的超时，实际作答会取消后续问题的无人值守时限。这些结果不决定模型下一步如何工作。

用户主动启用的 `/goal`、Workflow、Stop hook 和 `plan_first` 保留。子 Agent 仍不递归、默认最多 50 轮，`pause_turn` 仍最多续接三次；本轮未改变这些执行边界。接口迁移见 [扩展迁移说明](EXTENSION_MIGRATION.md)。

## 只追加的请求

在同一个会话内（两次压缩之间）：

- `system` 和 `tools` 逐字节不变；
- 每次请求的 `messages` 都是上一次请求的前缀加上新增内容。

适配器合并相邻同角色消息的方式是确定性的，所以合并结果每次相同。这样提示缓存可以命中，Claude 的 thinking 块也能保持有效。

`tests/harness/test_session.py::test_requests_are_append_only` 会验证这一点。

每次模型调用前检查压缩条件，所有 Provider 默认阈值为模型上下文窗口的 80%（`compaction.triggerRatio=0.8`）；显式 `triggerTokens` 优先，旧 `reserveTokens` 不再使用。`/compact` 可手动触发，overflow 时另尝试一次压缩。

- OpenAI API 使用 `/responses/compact`，把完整返回窗口保存为 `RawContent`，后续经 Responses API 原样回传；原 Chat Completions 连接在压缩后自动改用 Responses。Codex 账号使用普通 Responses 请求附加 `compaction_trigger`，保留用户输入和返回的唯一压缩项。
- Anthropic 使用当前模型的按需压缩（`compact-2026-09-04`、`compaction.type=summarize`），仅提交较早消息，保存完整明文摘要和签名；后续请求继续携带 beta header。
- 其他 Provider 使用小模型续接摘要，保留原始目标、后续修正、约束、进展、未解决事项和关键事实，结构由模型选择；支持旧摘要增量更新、文件列表和重点提示。第三方仅兼容 OpenAI/Anthropic 协议不会自动启用原生压缩。
- Anthropic 和通用摘要按 `keepRecentTokens` 选择切点，工具调用与结果保持在同侧；必要时补回最新用户请求。OpenAI 的完整返回窗口不再自行裁剪。
- 通用摘要先完整序列化已有的用户、助手及工具文本，不对单次参数或结果预裁剪；超过 600,000 字符总预算时保留首尾并插入遗漏标记，标记计入预算。这里不会重新读取已被工具输出预算截断的原始文件。
- 通用摘要剥离近期 thinking 并重建 system；原生压缩不重建 system。Anthropic 在连续保留、角色边界合适且模型/system/tools 配置不变时保留近期 thinking；补回请求或前缀配置改变时剥离。配置指纹随压缩记录保存，恢复后也会核对。
- 向会话文件追加 `compaction` 条目，原始消息不删除；原生压缩失败、取消或返回不完整结果时不替换历史。
- 原生状态不跨 Provider 发送；切换到不兼容 Provider 时恢复当前分支完整消息，并重新估算上下文。摘要调用和被保留消息的旧 usage 不用于压缩后的计数。

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
api.notify(text, mode="steer" | "follow_up")
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
