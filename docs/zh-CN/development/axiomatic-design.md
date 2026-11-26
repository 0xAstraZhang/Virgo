# 功能需求与 MIT 公理设计分析

[文档中心](../README.md) · [架构概览](architecture.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/development/axiomatic-design.md)

本文档记录 Virgo 的**功能需求（FRs）**、**系统约束（Cs）**以及 **MIT 公理设计**映射（$\{FR\} = [A]\{DP\}$），并明确区分 CI 中强制校验的**静态导入依赖矩阵（DSM）**与**功能-物理设计矩阵** $[A]$。

## 客户需求与系统约束

在公理设计中，全局非功能边界属于**约束（Constraints, Cs）**而非功能需求——所有设计参数（DP）都必须共同满足约束，约束之间不要求彼此独立：

- **CA0（总客户需求）**：在终端中提供一个轻量、可扩展、跨平台且低摩擦的编码代理，能够在保护隐私凭据与控制配置的前提下协助探索代码、修改文件、执行命令并持续推进复杂任务。
- **C1（护栏威胁模型）**：仅针对良性模型的偶发失误设防；恶意代码隔离、蓄意逃逸与提示注入链防御不在范围内（[ADR 001](../decisions/001-guardrail-threat-model.md)）。
- **C2（延迟与复杂度预算）**：Tier 1 平台冷启动 + 首次 Shell `<= 5s`，重复短 Shell `<= 2s`（`O(1)`），Broker `read` `<= 250ms`（`O(1)`）；未配置可选 SDK（如 MCP）时零冷启动导入开销（[原生执行与性能验收](execution-acceptance.md)）。
- **C3（Tier 1 跨平台支持）**：Linux（`bubblewrap`）、macOS（Seatbelt）、非提权原生 Windows 11（标准 AppContainer）及 Windows + WSL2，Python 3.11+（[ADR 003](../decisions/003-native-sandbox-backends.md)）。
- **C4（显式失败、禁止静默降级与自动重放）**：原生沙箱不可用时 fail-closed（交互会话仅提供单次显式 **No-Limit** 确认且不改变会话模式）；payload 一旦可能启动绝不自动重放（[ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)）。
- **C5（故障域隔离）**：插件/扩展加载失败、诊断写入失败或 UI 刷新异常均不得损坏会话历史或阻断核心启动（[ADR 005](../decisions/005-runtime-layering-and-durable-state.md)）。
- **C6（Prompt Cache 前缀稳定）**：普通轮次只追加；系统提示词与压缩后保留段落在两次压缩边界之间保持冻结（[ADR 006](../decisions/006-three-layer-provider-agnostic-compaction.md)）。

## 功能需求分解（FR0 → Level 1 → Level 2）

- **FR0**：在用户工作区中安全、可持续地协同完成软件工程任务。
- **DP0**：分层终端编码代理运行时（`src/virgo/`，Layer 0–5，由 `AgentSession` 装配协调）。

| Level-1 FR | Level-2 子功能需求（Sub-FRs） | 设计参数（DP） |
| --- | --- | --- |
| **FR1：接入并流式调度多提供商 LLM 与认证** | `FR1.1` 统一流式模型适配（`anthropic`、`openai`、`openai-responses`、`openai-codex`、`faux`）<br>`FR1.2` OAuth 与 API Key 凭据管理（`/login`、`/logout`）<br>`FR1.3` 模型目录发现/缓存、推理强度映射与辅助小模型路由（`/model`、`/effort`）<br>`FR1.4` 提供商错误分类（`retryable`、`overflow`、能力不支持、`fatal`） | **DP1**：`virgo.ai/*`（`providers/*`、`auth`、`oauth`、`catalog`、`models`）+ `virgo.harness.model_registry` |
| **FR2：实施拒绝优先权限边界、原生沙箱与项目信任治理** | `FR2.1` 三种执行模式（`read-only`、`workspace-edit`、`no-limit`）、不可覆盖凭据硬拒绝与沙箱环境变量过滤<br>`FR2.2` Tier 1 原生 OS 沙箱编译（`bubblewrap`、Seatbelt、Windows 标准 AppContainer）<br>`FR2.3` 按需资源审批、同轮拒答记忆与项目级持久授权（`request_permissions`、`permission-grants.sqlite3`）<br>`FR2.4` 高危 Shell 命令单次确认与原生沙箱不可用时交互式单次 No-Limit 降级<br>`FR2.5` 不可信项目配置合并前剥离、项目提示词符号链接拒读与 MCP 配置指纹绑定审批 | **DP2**：`execution_policy`、`execution_types`、`sandbox/*`、`execution`、`harness/{settings,execution,resource_approvals,resource_rules,high_risk}` |
| **FR3：执行工作区文件操作、Shell 命令与有界输出存储** | `FR3.1` Broker 直接文件工具（`read`、`write`、`edit`、`glob`、`grep`、`present_file`，`O_NOFOLLOW`、原子替换、`.gitignore`）<br>`FR3.2` Shell 解释器发现、绑定与进程树监管（`/shell`、`bash`/`powershell`、`HostProcess`、`WindowsProcess`）<br>`FR3.3` 有界工具输出落盘与分页/正则检索（`OutputStore`、`read_output`、7 天 TTL） | **DP3**：`file_access`、`process`、`output_store`、`tools/{fs,shell,bash,shell_env,output}`、`harness/shell_commands` |
| **FR4：编排多轮代理推理、工具调度与运行控制** | `FR4.1` 无状态 ReAct 循环、参数 Schema 校验及有界并发/串行工具调度<br>`FR4.2` 运行中转向（`steer`）、跟进队列、轮次/请求预算、截断续写与取消中断<br>`FR4.3` 指数退避重试与备用模型切换 | **DP4**：`virgo.agent/*`（`loop`、`agent`）+ `virgo.harness.{runtime,runtime_limits}` |
| **FR5：持久化分支会话、组装提示词并执行上下文压缩** | `FR5.1` 带文件锁的追加式 JSONL 会话树、断尾修复与会话切换/回收（`/new`、`/sessions`、`--continue`、`-r`、`session_info`）<br>`FR5.2` 不可变任务归属与分支锚定（`ExecutionOrigin`/`BranchSource`）及用量遥测（`request_header`）<br>`FR5.3` 分层系统提示词、项目指令、Git/工作区快照与提示词模板<br>`FR5.4` 提供商无关三层上下文压缩（`CompactionController` L0/L1/L2/L3、`/compact`） | **DP5**：`harness/{store,origin_context,prompt,compaction,compaction_records,telemetry}` |
| **FR6：捕获、隔离持久化并按作用域提供故障诊断证据** | `FR6.1` 无 I/O 错误捕获、凭据脱敏与应急落盘兜底<br>`FR6.2` 独立 WAL SQLite 诊断库与用户查询（`/diagnostics`）<br>`FR6.3` 运行时作用域证据投影、请求边界与压缩后检查点投递及显式声明查询工具（`diagnostic_get`） | **DP6**：`diagnostics`、`harness/{diagnostics,diagnostic_store,diagnostic_access,diagnostic_view,recovery}`、`plugins/diagnostic_get` |
| **FR7：委派隔离的子代理并监管后台异步作业** | `FR7.1` 可复用的会话级子代理，带独立轨迹存储、轮次上限与推理强度覆盖（`agent`、`SubagentStore`、`/subagents`）<br>`FR7.2` Core 托管的后台 Shell/子代理生命周期、查询、终止与归属会话唤醒（`JobManager`、`job_output`、`job_kill`、`/jobs`） | **DP7**：`harness/{jobs,subagents}`、`plugins/{subagent,background}` |
| **FR8：集成插件、扩展、钩子、MCP、清单、联网检索与澄清** | `FR8.1` 事务式内置插件、可覆盖用户/受信任项目 Python 扩展与 Shell 命令钩子（`EventBus`/`ExtensionAPI`、`ToolRegistry`）<br>`FR8.2` 技能发现与按需调用（`SKILL.md`、`/skills`、`/skill:name`）<br>`FR8.3` 按需延迟加载的 MCP stdio/HTTP/SSE 客户端（`mcp__*`、`mcp_status`、`/mcp`）<br>`FR8.4` 模型自维护清单与跨压缩周期冻结保留（`todo_write`）<br>`FR8.5` 宿主/客户端 Web 搜索与抓取能力规划及降级（`web_search`、`web_fetch`、`/search`、`/fetch`）<br>`FR8.6` 模型主动向用户澄清及无人值守超时（`ask_user`） | **DP8**：`harness/{extensions,tool_registry}`、`plugins/{skills,web/*,ask_user}`、`extensions/{mcp,todo}`、`tools/{web,web_fetch}` |
| **FR9：提供多模式终端界面、命令入口与实时双语本地化** | `FR9.1` 全屏 Textual TUI（流式转录、折叠、审批/偏好/会话/子代理选择器、多工作区切换）<br>`FR9.2` 纯文本 REPL 与非交互 Print/JSON 输出（`-p --format json`）<br>`FR9.3` 显式能力声明 `UI` 接口、斜杠命令路由与运行时双语切换（`en`/`zh-CN`、`/language`、`/status`、`/help`） | **DP9**：`virgo.cli/*`（`cli/tui/*`、`repl`、`console`、`main`）、`harness/{ui,commands,preferences,auth_commands}`、`virgo.i18n/*` |

## 静态依赖矩阵（DSM）与功能-物理设计矩阵（`[A]`）对比

Virgo 明确区分两种互补的结构视图：

1. **静态代码依赖矩阵（DSM）—— 严格下三角**：
   由 `tests/test_architecture_matrix.py` 在 CI 中以 AST 强制校验：
   - **包级零环、全量模块导入零环**（同时覆盖顶层、函数局部及 `TYPE_CHECKING` 导入），借助 `virgo.harness.contracts` 的窄协议消除 `harness/` 内部类型回指环。
   - **严格单向包层级**（Layer 0 → 1 → 2a → 2b → 3 → 4 → 5）。
   - **同级块对角独立**：AI 提供商族、执行叶子组件（`file_access`、`output_store`、`sandbox`）、内置工具族（`fs`、`shell`、`web`、`web_fetch`）、内置插件与内置扩展彼此零横向导入。
   - **单向服务与呈现契约**：`ShellSelection` 仅接收由 `JobManager` 推送的可选 `BackgroundLauncher`（`JobManager` 仅通过注入回调获取默认 Shell，彻底断开 `Shell ↔ Jobs` 环）；`CompactionController` 与 `Runtime` 通过 `ExtensionAPI.add_request_fallback(..., matches=...)` 识别请求扩展回退错误而不硬编码 Web 错误码；诊断查询能力通过 `ExtensionAPI.declare_diagnostic_query_tool(...)` 声明而不硬编码工具名；`cli/` 仅通过 `AgentSession`/`Runtime` 公共门面交互。

2. **功能-物理设计矩阵 `[A]`（`{FR} = [A]{DP}`）—— 宏观近下三角，保留有界领域闭环**：

> 图例：**X** = 主对角实现元；`x` = 合法下三角依赖（$j < i$）；**⊗** = 有界上三角运行时反馈（$j > i$）；`·` = 无直接依赖。

| FR \ DP | DP1 | DP2 | DP3 | DP4 | DP5 | DP6 | DP7 | DP8 | DP9 |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **FR1** 模型接入与认证 | **X** | **⊗** | · | **⊗** | · | · | · | · | · |
| **FR2** 权限沙箱与配置信任 | · | **X** | **⊗** | · | **⊗** | · | · | **⊗** | **⊗** |
| **FR3** 文件/Shell/输出 | · | `x` | **X** | · | · | · | · | · | · |
| **FR4** 代理推理与循环 | `x` | `x` | `x` | **X** | **⊗** | **⊗** | · | **⊗** | · |
| **FR5** 会话/提示/压缩 | `x` | `x` | `x` | `x` | **X** | **⊗** | · | **⊗** | · |
| **FR6** 诊断与恢复证据 | `x` | `x` | `x` | `x` | `x` | **X** | · | · | · |
| **FR7** 子代理与后台作业 | `x` | `x` | `x` | `x` | `x` | `x` | **X** | · | · |
| **FR8** 扩展/MCP/Todo/Web/Ask | `x` | `x` | `x` | `x` | `x` | · | `x` | **X** | **⊗** |
| **FR9** 终端界面与本地化 | `x` | `x` | `x` | `x` | `x` | `x` | `x` | `x` | **X** |

### 有意保留的领域反馈闭环

为避免引入无谓抽象而推高信息量与维护复杂度（公理二），Virgo 将以下属于问题域本质的反馈闭环显式收敛在窄接口之后：

- **`FR4（Runtime）` ↔ `FR5（压缩与存储）`**：L2 缓存热压缩有意复用活跃运行时的 `request_context()` 与预算（C6），而持久化异常通过中止信号 fail-closed 停止循环（C4/C5）；双方通过窄协议 `CompactionHost` 与存储失败回调收口。
- **`FR2（审批）` ↔ `FR9（UI）` 及 `FR8（ask_user）` ↔ `FR9（UI）`**：交互审批、一次性 No-Limit 降级与用户澄清本质上是暂停执行等待人类反馈的闭环，通过显式 `UI` 能力标志（`interactive`、`supports_confirm`、`supports_confirm_once`、`confirmation_handler()`）约束。
- **`FR2（权限）` ↔ `FR5（Origin/Store）` 及 `FR2` ↔ `FR3（路径解析）`**：授权与审计绑定到 `ExecutionOrigin`/`BranchSource` 修订号，持久化失败阻断工具准入，审批弹窗通过 `file_access` 解析规范路径。
- **`{FR4, FR5}` ↔ `FR6（诊断恢复）`**：`RuntimeDiagnostics` 在自然请求边界与压缩检查点投递诊断证据，不额外触发模型轮次。
- **`{FR2, FR4, FR5}` ↔ `FR8（钩子 / Web / Todo）`**：工具钩子围绕显式权限边界（`priority=100`）执行且存在工具钩子时自动禁用宿主 Web 报价；请求预处理、回退匹配与请求系统段统一经由 `ExtensionAPI` 通用钩子参与请求构建与压缩回退。
- **`FR1（模型目录/配置）` ↔ `FR2（项目信任）` 及 `FR4（运行态）`**：不可信项目的 `providers` 在合并前被剥离，后台模型目录元数据仅在 `AgentSession` 空闲时合并，避免运行中替换模型对象破坏在途请求绑定。

另见：[架构概览](architecture.md) · [执行接口](execution-interface.md) · [ADR 005](../decisions/005-runtime-layering-and-durable-state.md)
