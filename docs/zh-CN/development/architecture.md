# 架构概览

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/development/architecture.md)

Virgo 在 `src/virgo/` 下按包层级组织静态可见的导入关系。这些边界支持模型适配、Agent 循环、执行护栏和会话运行时分别实现与测试。运行时装配、回调和共享资源仍需行为契约；导入图无环不等于功能独立。

## 包依赖分层

| 层级 | 包 / 模块 | 核心职责 |
| --- | --- | --- |
| **0. 叶子基础层** | `_gitignore`、`_win32`、`i18n`、`diagnostics`、`execution_policy` | 纯 Python `.gitignore` 匹配、共享 Win32 绑定、会话级本地化（`en`、`zh-CN`）、无 I/O 错误采集与脱敏，以及不依赖沙箱或运行时的 deny 优先 `PermissionPolicy` 与内置执行模式 |
| **1. 契约、进程与核心层** | `execution_types`、`process`、`ai`、`agent` | 执行契约与异常类型（`ExecutionContext`、`ProcessRequest`、`ProcessHandle`）、POSIX `HostProcess` 与 Windows `WindowsProcess` 原语、统一流式 LLM 适配层（`anthropic`、`openai`、`openai-responses`、`openai-codex`、`faux`）、OAuth/API 认证、模型目录，以及无状态 `run_loop` / `Agent` / `AgentTool` 编排（含非顺序工具批次的有界并发执行） |
| **2a. 执行叶子组件** | `file_access`、`output_store`、`sandbox` | 彼此独立的 Broker 文件 I/O（末级 `O_NOFOLLOW` + 原子 `os.replace`）、会话级 Broker 输出存储（`OutputStore`），以及各平台原生沙箱适配器（`bubblewrap`、macOS Seatbelt、Windows 标准 AppContainer） |
| **2b. 执行装配门面与工具** | `execution`、`tools` | 统一组合门面（`HostContext`、`SandboxContext`、`SandboxExecutor`、`is_windows_sandbox_supported`、`default_execution_mode`）及核心工具实现（`fs`、`shell_env`/`shell`/`bash`、`output`、`web`、`web_fetch`） |
| **3. 会话运行时** | `harness` | `AgentSession`（组合 `ToolRegistry` 与 `ModelRegistry`）、`Runtime`、`EventBus`/`ExtensionAPI`（含通用请求预处理与回退钩子）、JSONL `SessionStore`、`ModeExecutionProvider`、`ResourceApprovals`/`ResourceRules`、`high_risk` 检查、Core 托管的 `JobManager`、SQLite `DiagnosticLog`、配置与信任加载、统一三层上下文压缩（`CompactionController` + `compaction_records`）、提示词组装、显式能力声明的 `UI` 接口及斜杠命令 |
| **4. 能力扩展层** | `plugins`、`extensions` | 通过 `setup(api)` 加载的 6 个内置插件（`diagnostic_get`、`skills`、`ask_user`、`subagent`、`background`、`web`）与 2 个内置扩展（`mcp`、`todo`） |
| **5. 交互呈现层** | `cli`、`__main__` | 命令行入口（`virgo.cli.main:main`）、逐行 REPL、Textual 全屏 TUI（`cli/tui/`）及非交互/JSON 输出界面 |

### 依赖边界约束

通过 `tests/test_architecture_matrix.py` 及 `tests/harness/test_permission_modes_e2e.py` 的 AST 校验强制执行。导入提取覆盖 `Import` 和 `ImportFrom`，包括函数局部与 `TYPE_CHECKING` 语句，不解析动态导入、回调或共享状态依赖：

- **可拓扑排序的静态 DSM**：运行时包图和模块图无环，模块图加入 `TYPE_CHECKING` 边后仍无环。顶层与函数局部的运行时导入遵守包层级，Layer 0 额外禁止 `TYPE_CHECKING` 向上导入。`virgo.harness.contracts` 的 Layer-3 协议无同级导入。这些性质允许对提取的图作下三角排序，不覆盖全部运行时交互。
- **纯策略与核心叶子**：`virgo.execution_policy` 不依赖 `virgo.sandbox`、`virgo.execution` 或 `virgo.harness`（Windows 沙箱能力探测归属 `virgo.execution`）；`virgo.ai` 与 `virgo.agent` 不导入 `execution*`、`process`、`file_access`、`output_store`、`sandbox`、`tools`、`harness`、`plugins`、`extensions` 或 `cli`。
- **特定同级导入边界**：在提取的图中，同级 AI 提供商家族（`anthropic`、OpenAI 系、`faux`）、执行叶子组件（`file_access`、`output_store`、`sandbox`）、内置工具族（`fs`、`shell`/`bash`、`web`、`web_fetch`）以及同级插件/扩展之间无跨族运行时导入。这不宣称行为独立。
- **显式跨边界契约**：受检查的仓库内插件/扩展使用 `ExtensionAPI`，不直接访问 `api.session` 或私有 `runtime._*`/`session._*`；受信任第三方 Python 扩展不受此源码扫描隔离。`ShellSelection` 接收 `JobManager` 推送的可选 `BackgroundLauncher`，后者经注入回调解析默认 Shell。请求扩展回退使用 `ExtensionAPI.add_request_fallback(..., matches=...)`。Runtime 在调用任何已注册的回退处理器之前检查提供商托管工作是否已经启动；已启动托管工作的失败请求不会自动重放。诊断查询声明提供候选名称；新工具结果、待投递通知、检查点与后台投影从当前运行时实际暴露工具中选择名称，不重写历史引用。`cli` 使用 `AgentSession`/`Runtime` 公共门面（`observe_events`、`attach_ui`/`detach_ui`、`cancel_signal`、`has_runnable_wakeups`、`run_concurrent_command`）。

[`harness/extensions.py`](../../../src/virgo/harness/extensions.py)在装配时动态加载插件和扩展，此类加载不在导入提取器范围内。[公理设计](axiomatic-design.md)说明稀疏叶级 FR–DP 影响分析及其边界，[叶级需求](functional-requirements.md)定义可观察结果，[功能验收](functional-acceptance.md)记录行为证据与适用边界。

## 核心工具、内置插件与内置扩展

Virgo 将运行时能力划分为三层，对应不同的覆盖优先级与故障隔离边界：

| 层级 | 加载方式与清单 | 禁用与覆盖规则 | 故障隔离 |
| --- | --- | --- | --- |
| **核心工具** | 在 `AgentSession.__init__` 中直接装配：`read`、`write`、`edit`、`glob`、`grep`、`present_file`（显示可读本地文件经核验的绝对路径）、`read_output`、`bash`/`powershell`，以及 `request_permissions`（无法交互审批时自动隐藏） | 不允许按文件名覆盖 | 核心初始化失败直接中止启动 |
| **内置插件**（`virgo.plugins`） | 由 `load_builtin_features()` 通过 `setup(api)` 加载：`diagnostic_get`、`skills`、`ask_user`、`subagent`、`background`、`web` | 按 ID 通过 `plugins.<id>.enabled` 控制（设为 `false` 时不导入）；不受 `--no-extensions` 影响；不允许按文件名覆盖 | 在可回滚事务中加载；单插件失败仅降级该插件（如 `background` 降级时，`shell` 与 `subagent` 自动去掉 `background` 参数） |
| **内置与自定义扩展**（`virgo.extensions` + 用户/项目扩展） | 由 `load_extensions()` 通过 `setup(api)` 加载：内置 `mcp`、`todo`，以及 `~/.virgo/extensions/*.py` 和受信任 `.virgo/extensions/*.py` | 按文件主名（stem）解析覆盖：`内置 < 用户 < 受信任项目`；可用 `extensions.disabled` 按名禁用，或用 `--no-extensions` 跳过全部扩展 | 同样通过 `setup(api)` 的可回滚事务隔离故障 |

## 运行时托管与任务隔离

- **`AgentSession` 与 `Runtime`**：`AgentSession` 持有用户设置、权限模式、扩展、持久化存储和 Core `JobManager`；`Runtime` 承载单条模型对话循环（主会话与子 Agent）。
- **不可变执行上下文**：每次任务启动时捕获原始的 `HostContext` 或 `SandboxContext` 以及锚定在当前分支叶节点的 `ExecutionOrigin`。子 Agent 与后台任务沿用该捕获上下文和分支归属，不受后续 UI 切换模式、分支或会话的影响。
- **Core 托管后台任务**：`JobManager` 统一负责后台任务的生命周期、完成通知与清理；`background` 和 `subagent` 插件只提供模型工具（`job_output`、`job_kill`、`agent`）和 `/jobs` 命令。

每个子代理单次运行最多使用 `subagents.maxTurns` 个 assistant 轮次（默认 20）。同一会话中，同一 `agent_id` 的所有运行合计最多使用 `subagents.maxTotalTurns` 轮（默认 40）。上下文压缩和会话恢复都会保留累计轮数。`subagents.effort` 默认为 `null`，即继承父级推理强度，也可以指定模型支持的档位。

Codex 账号适配器的请求共享会话 HTTP 连接池。只有在账号、源和端点路径均相同时，才会回传 `__oailb` 和 `__cflb` 路由 Cookie。轮次状态还按运行时隔离。每个 I/O 任务读取自己的 HTTPS 事件流并关闭响应。协议终态事件可能先于 HTTP 响应体结束；正常完成时适配器会在有界且响应取消的短窗口内排空尾部块与 EOF 以复用长连接，并在清理完成后再返回结果或结束取消操作。

## 本地状态目录布局

默认情况下，Virgo 将用户数据保存在 `~/.virgo`（可通过 `VIRGO_HOME` 覆盖）：

| 路径 | 用途 |
| --- | --- |
| `settings.json`、`auth.json`、`mcp.json` | 用户设置、OAuth/API 凭据及用户级 MCP 服务器配置 |
| `AGENTS.md`、`SYSTEM.md`、`APPEND_SYSTEM.md` | 用户级提示词上下文与系统提示词覆盖 |
| `extensions/`、`skills/`、`prompts/` | 用户 Python 扩展、`SKILL.md` 技能及 Markdown 斜杠命令模板 |
| `sessions/<workspace-slug>/` | 追加写入的 JSONL 会话树（`session`、`message`、`compaction`、`model_change`、`leaf`、`custom` 记录），带跨平台文件锁与恢复时截断尾部自动修复 |
| `outputs/session-<id>/` | Broker 私有的 `OutputStore` 输出存储（`out_<12-hex>`、通过 `read_output` 支持模式搜索、尾部读取、按行分页及按 UTF-8 字符边界字节分页、7 天 TTL、随会话回收清理） |
| `logs/diagnostics.sqlite3` | WAL 模式持久化诊断错误库（与会话存储故障隔离；通过 `diagnostic_get` 向模型暴露当前运行时范围的诊断证据） |
| `permission-grants.sqlite3` | 持久化的**始终允许**资源授权（`resource_grants` 表，按 `(project, mode, resource, target)` 索引） |

主代理可通过 `session_info` 获取与 `/session` 相同的会话 ID 和轨迹文件路径。仅保存在内存中的会话返回空路径（`null`）。

主模型首次请求（含恢复会话后）和配置变化时，在适配器分派前追加 `custom/request_header`，记录提供方、模型、API、请求的推理强度、协议发送值、解析后的有效值及输出上限。用量继续记在助手消息上。

在原生 Windows 上，AppContainer 会话租约日志单独保存在 `%LOCALAPPDATA%\Virgo\sandbox-sessions\`（可通过 `VIRGO_WINDOWS_SANDBOX_STATE_DIR` 覆盖）。

另见：[功能需求与公理设计分析](axiomatic-design.md) · [执行接口](execution-interface.md) · [原生执行与性能验收](execution-acceptance.md) · [架构决策索引](../decisions/README.md)

## 评测遥测

真实模型对照评测的两组均使用 `openai-codex/gpt-6.2-sol` 与 `xhigh`，包括压缩与回退调用。测量前固定两版提交、用例和验收标准，并在服务商请求边界核对实际发送的模型和推理档位。

打印模式（`-p --format json`）在关闭会话后追加带版本的 `run_telemetry` 记录。统一 AI 流接口在供应商派发边界观测模型请求，包括子 Agent、重试和压缩，与历史会话统计无关。构造完成的 HTTP 请求到达传输层发送钩子时计入遥测。每次请求记录 ID、用途、Agent、供应商、API、模型、推理强度、耗时、停止原因和供应商报告的 Usage（未知字段为 null）。Faux 为模拟调用，不提供实际 Token。输入总量包含缓存读写；供应商报告的推理 Token 是输出的子集。不推算费用。本地工具调用数包含被拒绝的调用，实际执行数排除未开始的调用；供应商托管工具不属于本地工具计数。缺少终态记录或记录不完整时，不能认定试验总量完整。初始化和执行时间仅覆盖 CLI 会话构造及提示词处理，不代表外部试验总耗时。已有会话历史 Usage 保留为独立的界面统计。
