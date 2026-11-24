# 架构概览

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/development/architecture.md)

Virgo 在 `src/virgo/` 下按单向依赖分层组织。底层模块不依赖会话状态或终端界面，使模型适配、Agent 循环、执行护栏和会话运行时可以独立演进与测试。

## 包依赖分层

| 层级 | 包 / 模块 | 核心职责 |
| --- | --- | --- |
| **0. 叶子基础层** | `i18n`、`diagnostics`、`execution_policy`、`output_store` | 会话级本地化（`en`、`zh-CN`）、无 I/O 错误采集与脱敏、deny 优先的 `PermissionPolicy` 与内置执行模式，以及 Broker 管理的会话级输出存储 |
| **1. 模型与 Agent 核心** | `ai`、`agent` | 统一流式 LLM 适配层（`anthropic`、`openai`、`openai-responses`、`openai-codex`、`faux`）、OAuth/API 认证、模型目录、原生压缩，以及无状态 `run_loop` / `Agent` / `AgentTool` 编排（含非顺序工具批次的有界并发执行） |
| **2. 执行层与基础工具** | `execution`、`file_access`、`sandbox`、`tools` | 统一 `SandboxExecutor` 与不可变 `HostContext`/`SandboxContext`、Broker 文件 I/O（末级 `O_NOFOLLOW` + 原子 `os.replace`）、各平台原生沙箱适配器（`bubblewrap`、macOS Seatbelt、Windows 标准 AppContainer），以及核心工具实现（`fs`、`shell`、`bash`、`output`、`web`、`web_fetch`） |
| **3. 会话运行时** | `harness` | `AgentSession`、`Runtime`、`EventBus`/`ExtensionAPI`、JSONL `SessionStore`、`ModeExecutionProvider`、`ResourceApprovals`/`ResourceRules`、`high_risk` 检查、Core 托管的 `JobManager`、SQLite `DiagnosticLog`、配置与信任加载、上下文压缩、提示词组装及斜杠命令 |
| **4. 能力扩展层** | `plugins`、`extensions` | 通过 `setup(api)` 加载的 6 个内置插件（`diagnostic_get`、`skills`、`ask_user`、`subagent`、`background`、`web`）与 2 个内置扩展（`mcp`、`todo`） |
| **5. 交互呈现层** | `cli` | 命令行入口（`virgo.cli.main:main`）、逐行 REPL、Textual 全屏 TUI（`cli/tui/`）及非交互/JSON 输出界面 |

### 依赖边界约束

- `virgo.ai` 与 `virgo.agent` 不导入 `execution*`、`sandbox`、`tools`、`harness`、`plugins`、`extensions` 或 `cli`。
- `virgo.execution_policy`、`virgo.execution`、`virgo.file_access`、`virgo.output_store`、`virgo.sandbox` 与 `virgo.tools` 不反向依赖 `harness` 或 `cli`（`virgo.sandbox` 不依赖 `virgo.harness` 由 `tests/harness/test_permission_modes_e2e.py` 进行 AST 强制校验）。
- `virgo.execution_policy` 保持不依赖 harness；仅在原生 Windows 上，`is_windows_sandbox_supported()` 延迟导入 `virgo.sandbox.windows_acl` 完成能力探测。

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

## 本地状态目录布局

默认情况下，Virgo 将用户数据保存在 `~/.virgo`（可通过 `VIRGO_HOME` 覆盖）：

| 路径 | 用途 |
| --- | --- |
| `settings.json`、`auth.json`、`mcp.json` | 用户设置、OAuth/API 凭据及用户级 MCP 服务器配置 |
| `AGENTS.md`、`SYSTEM.md`、`APPEND_SYSTEM.md` | 用户级提示词上下文与系统提示词覆盖 |
| `extensions/`、`skills/`、`prompts/` | 用户 Python 扩展、`SKILL.md` 技能及 Markdown 斜杠命令模板 |
| `sessions/<workspace-slug>/` | 追加写入的 JSONL 会话树（`session`、`message`、`compaction`、`model_change`、`leaf`、`custom` 记录），带跨平台文件锁与恢复时截断尾部自动修复 |
| `outputs/session-<id>/` | Broker 私有的 `OutputStore` 输出存储（`out_<12-hex>`、通过 `read_output` 按 UTF-8 字符边界分页、7 天 TTL、随会话回收清理） |
| `logs/diagnostics.sqlite3` | WAL 模式持久化诊断错误库（与会话存储故障隔离；通过 `diagnostic_get` 向模型暴露当前运行时范围的诊断证据） |
| `permission-grants.sqlite3` | 持久化的**始终允许**资源授权（`resource_grants` 表，按 `(project, mode, resource, target)` 索引） |

主代理可通过 `session_info` 获取与 `/session` 相同的会话 ID 和轨迹文件路径。仅保存在内存中的会话返回空路径（`null`）。

主模型首次请求（含恢复会话后）和配置变化时，在适配器分派前追加 `custom/request_header`，记录提供方、模型、API、请求的推理强度、协议发送值、解析后的有效值及输出上限。用量继续记在助手消息上。

在原生 Windows 上，AppContainer 会话租约日志单独保存在 `%LOCALAPPDATA%\Virgo\sandbox-sessions\`（可通过 `VIRGO_WINDOWS_SANDBOX_STATE_DIR` 覆盖）。

另见：[执行接口](execution-interface.md) · [原生执行与性能验收](execution-acceptance.md) · [架构决策索引](../decisions/README.md)
