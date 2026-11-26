# 005 — 运行时分层、扩展机制边界与本地持久化存储

[决策索引](README.md) · [文档中心](../README.md) · [English](../../en/decisions/005-runtime-layering-and-durable-state.md)

**状态：** 已落地

## 背景

随着 Virgo 支持多模型厂商、子 Agent、后台任务、MCP 服务器、上下文压缩和全屏 TUI，如果界面状态、模型编排、执行后端与可选功能彼此耦合，会同时拖慢冷启动、增加崩溃恢复风险，并令模块测试难以独立运行。

## 决策

- **严格下三角分层与同级模块对角独立**：`src/virgo/` 在包级与模块级均保持运行时无环（同时覆盖顶层与函数内局部导入），按严格单向分层组织：Layer 0 纯叶子基础层（`_gitignore`、`_win32`、`i18n`、`diagnostics`、`execution_policy`）→ Layer 1 执行契约/进程原语与核心层（`execution_types`、`process`、`ai`、`agent`）→ Layer 2a 彼此独立的执行叶子组件（`file_access`、`output_store`、`sandbox`）→ Layer 2b 执行装配门面与工具（`execution`、`tools`）→ Layer 3 `harness` → Layer 4 彼此独立的 `plugins`/`extensions` → Layer 5 `cli`。同级 AI 提供商家族、执行叶子组件、内置工具族以及插件/扩展之间互不依赖，并通过 `tests/test_architecture_matrix.py` 在 CI 中以 AST 强制校验分层无环与显式跨边界契约（包括 `ExtensionAPI` 通用请求预处理/回退钩子、`ToolResult.error_kind`、显式 `UI` 确认能力声明及 `AgentSession` 门面）。
- **三层能力边界**：
  - **核心工具**（`fs`、`shell`、`output`、`request_permissions`）由 `AgentSession` 直接装配。
  - **内置插件**（`virgo.plugins`：`diagnostic_get`、`skills`、`ask_user`、`subagent`、`background`、`web`）经 `setup(api)` 在事务中加载，通过 `plugins.<id>.enabled` 开关，不可按文件名覆盖，也不受 `--no-extensions` 影响。
  - **内置扩展**（`virgo.extensions`：`todo`、`mcp`）走标准用户/项目扩展解析管道（`内置 < 用户 < 受信任项目`），可通过 `extensions.disabled` 或 `--no-extensions` 禁用，并严格控制冷启动成本（例如 `mcp` 仅在配置了至少一个服务器时延迟导入 MCP SDK）。
- **Core 统一托管后台任务**：`AgentSession`（组合 `ToolRegistry` 与 `ModelRegistry`）与 `JobManager` 负责会话存储、生命周期通知和进程/任务清理，`Runtime` 专注执行单条对话循环，插件仅通过 `ExtensionAPI` 暴露用户命令和模型工具。
- **按职责拆分的故障隔离持久化存储**：
  - **会话树（`sessions/<workspace-slug>/*.jsonl`）**：带文件锁的追加写入 JSONL 树，可写恢复时自动截断末尾未换行的不完整记录；持久化异常时 fail-closed 抛出 `SessionSaveError` 且不丢弃内存历史。
  - **命令与 MCP 输出（`outputs/session-<id>/`）**：Broker 私有 `OutputStore`（`out_<12-hex>`、按 UTF-8 边界分页的 `read_output`、7 天 TTL、未截断前台命令立即删临时文件），让大段输出可跨恢复查阅而不膨胀对话历史。
  - **子 Agent 轨迹（`subagents/session-<id>/`）**：按主会话隔离的 `SubagentStore`（基于 `SessionStore` 的 `subagent-<n>.jsonl`），将子 Agent 执行过程与主对话解耦，同时支持同一会话内跨任务复用子 Agent 以及按需查看实时/历史轨迹（含会话恢复后），并在删除主会话时一并清理。
  - **错误诊断（`logs/diagnostics.sqlite3`）**：WAL 模式 SQLite，与会话写入完全隔离，区分常规工具错误与运行时故障，且模型侧 `diagnostic_get` 仅能读取当前运行时记录过的 `error_id`。
  - **持久授权（`permission-grants.sqlite3`）**：单一 `resource_grants` 表，按 `(project, mode, resource, target)` 存储。

## 取舍与约束

- **重型可选依赖必须延迟导入**：内置扩展和可选功能在未配置时不得拖累冷启动预算。
- **禁止反向依赖 `harness` 或 `cli`**：核心策略、执行层、沙箱和 Agent 模块必须无需构造 TUI 或完整 `AgentSession` 即可独立测试。
- **隔离故障域**：诊断日志写入失败绝不能破坏会话保存，单个插件初始化失败必须干净回滚而不导致启动崩溃。

## 相关文档

- [架构概览](../development/architecture.md)
- [内置扩展（Todos 与 MCP）](../guides/extensions.md)
- [执行接口](../development/execution-interface.md)
