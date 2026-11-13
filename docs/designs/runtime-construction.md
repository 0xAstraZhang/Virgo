# Runtime 创建与会话归属计划

状态：待实施。核对基线：`76e0213`。本计划先消除构造主 Runtime 时临时替换活动 store 的行为，再收拢内置子任务的创建入口。

目标是沿构造参数确定消息存储、用量记账、工具范围和权限审查来源。会话切换先准备完整候选 Runtime，成功后才提交活动状态；构造失败不改变原会话。保存失败修复无需等待本计划。

## 当前边界

[`Runtime.__init__()`](../../src/virgo/harness/session.py) 已经接收 `store`，但 `accounting_store` 默认取 `session.store`。因此，仅向主 Runtime 传入目标 store，仍可能使消息写新会话、用量记旧会话。

`_prepare_main()` 为配合 `_new_main()` 和上述默认值，暂时替换 `session.store`。构造期间 `build_system()` 会调用扩展的 system section，此时回调可能同时观察到新 `session.store` 和旧 `session.main`。持有 Session 锁不能解决同线程回调看到的混合状态。

队友和子 Agent 直接调用 `Runtime(...)`，但都已经接入统一工具回调。子任务的 `review_parent` 也已绑定原任务分支；本计划保留这些行为，不把直接构造等同于绕过权限。

## 显式构造参数

先把 `_new_main()` 改为接收目标 store，从目标 store 构建消息和 ID；删除 `_prepare_main()` 中对活动 `session.store` 的临时赋值。

为 Runtime 明确提供记账目标，避免在候选构造时读取当前活动 store。内置创建遵循以下绑定：

| Runtime | 消息存储 | 记账目标 | 审查来源 |
| --- | --- | --- | --- |
| 主 Agent | 显式传入的目标 store | 同一个目标 store | 由目标 store 创建根来源 |
| 队友 | 队友独立 store；正常内存模式下路径为空 | 创建时捕获的父来源根分支 | 显式传入的父来源与队友自身历史 |
| 子 Agent | 保持 `None`，不新增独立 JSONL | 创建时捕获的父来源根分支 | 显式传入的父来源与独立内存历史 |

不要把 `store=None` 的子 Agent 自动绑定到主会话消息存储。记账 store 与消息 store 是两个不同职责，即使主 Agent 通常使用同一对象，也不能在子任务里合并。

根分支仍使用现有 `BranchSource`，保留迟到审计写入的 `active_leaf` 行为，不让后台任务移动用户当前选择的分支。

## Harness 创建入口

在 `AgentSession` 提供小型 `create_runtime(...)` 入口，集中解析角色工具集、显式 store、记账目标和父审查来源。`Runtime` 保持对单个对话的实现，不增加运行时注册中心或通用依赖注入容器。

角色业务仍留在 feature：队友名称、worktree、收件箱、线程和计划模式由 Team 管理；AgentType、委派说明、后台任务和结果包装由 Subagents 管理。创建入口只负责共同绑定及校验。

内置子任务在排队前捕获父 `ReviewSource` 并固定根分支，真正启动时传入同一对象。已有 `ctx.review_source` 优先，不在后台线程启动时重新读取 `session.main`。Workflow 通过 Subagents 创建 Runtime 的路径也一并核对。

工具集合继续使用角色范围与 AgentType 白名单的交集，保留显式空列表与未指定工具的区别。同名工具替换、shell 绑定、模型覆盖、`max_turns`、`signal`、压缩开关及 system extra 均保持现有语义。

所有新建 Runtime 在完成工具范围和审查来源绑定后才对外可用，并接入相同的 Harness 预检、复核和结果回调。本计划可使用现有回调接口，也可在权限接口重构合入后接新接口，不重复实现权限规则。

## 构造期间的扩展上下文

移除临时 store 替换会影响读取 `api.session.store` 的 system section，不能只改构造函数后认为兼容性已经完成。

为需要目标会话数据的 system section 增加显式、只读的构造上下文，包含角色、cwd、目标消息 store、记账目标和父审查来源。调用 `build_system()` 时由创建入口传入。新增回调通过明确的注册入口接收上下文，不用捕获 `TypeError` 猜测回调签名。

旧的 `fn(role)` 回调继续收到角色字符串，未读取会话内部状态的扩展保持兼容。依赖候选 store 的扩展改用新上下文；`api.entries()` 和 `api.session.store` 仍表示已激活会话，不通过线程局部变量或临时替换 Session 对象暗中改变其含义。这一迁移要求写入扩展文档，并提供读取目标 store 自定义记录的短示例。

候选 Runtime 传递的是明确目标，旧 `session.store` 与旧 `session.main` 在激活前保持配套。构造上下文不授权回调在准备阶段修改活动会话，准备失败也不得留下候选会话的记账写入。

## 会话激活与生命周期

`_activate_store()` 保持准备与提交两个阶段：加载并校验目标记录、创建候选 Runtime、完成系统提示构造后，才在 Session 锁内更新活动 store 和 main，并在两者一致后发出一次 `session_start`。涉及后台读取方时统一通过受保护的访问方式读取配套状态，不把两次 Python 赋值描述为对所有线程天然原子。

准备失败时保留原 store、main、已访问存储列表和可用状态。激活前继续检查 shutdown 状态；准备期间发生关闭时丢弃候选，不启动任何候选任务。

回收当前会话时仍先准备替代 Runtime，再执行回收站操作；任一步失败都不能误切换会话。保留对重复打开 store 的处理，防止迟到写入重新创建已回收的文件。

TUI 多个 ConversationPane 继续持有各自的 AgentSession，切回运行中会话继续复用原运行实例。此计划不将多会话改成一个全局 Session，也不借工厂改变任务停止和关闭的所有权。

## 实现落点

| 位置 | 修改内容 |
| --- | --- |
| `harness/session.py` | 显式主 store 与记账绑定、创建入口、候选准备与激活 |
| `harness/prompt.py`、`harness/extensions.py` | 显式构造上下文、system section 注册兼容与迁移 |
| `features/team.py`、`features/subagent.py` | 迁移到统一入口，保留 feature 自有生命周期 |
| `features/workflow.py`、`features/permission_context.py` | 核对排队前来源捕获、根分支和迟到审计 |
| `harness/compaction.py`、辅助模型调用方 | 使 Runtime 相关的摘要、重试和压缩用量使用已绑定的记账目标 |

特别检查通用压缩调用 `session.complete()` 时未传 `accounting_store` 的路径，避免主构造已显式绑定、辅助调用却又回落到当前 `session.store`。只修正与 Runtime 归属相关的调用，不顺带重构所有后台服务。

## 实施顺序与验收

1. 先列出目标 store、记账目标、审查根分支及扩展回调的对应关系，复用已有会话切换和子任务场景。对候选构造失败、混合状态和辅助记账等必要缺口，先写真实 Session 端到端回归，再修改对应实现；不在实现后补测试。
2. 显式传入主 Runtime 的 store 和记账目标，补齐扩展构造上下文，然后删除临时活动 store 替换。
3. 提取共同创建入口，迁移 Team 和 Subagents；逐项保持现有参数与生命周期。
4. 核对辅助记账及关闭、回收、切换路径，更新架构和迁移文档，再运行受影响场景。

| 验收场景 | 必须观察到的结果 |
| --- | --- |
| 为会话 B 准备主 Runtime，当前活动会话为 A | 候选消息、记账、审查均归 B；活动 A 的 store 与 main 始终配套 |
| 目标记录非法、构造异常或准备期间关闭 | A 保持可用或按既定关闭流程结束；不产生半激活候选 |
| system section 读取目标会话数据 | 新上下文得到 B；旧角色回调兼容；不观察到新 store 与旧 main 混合 |
| 后台子 Agent 排队后切换会话或分支 | 后续审查、重试和用量仍归原任务；不改变新分支叶子 |
| 队友使用 worktree、plan_first、独立历史 | cwd、计划权限、消息路径和用量归属符合绑定表 |
| 空工具列表、AgentType 白名单、角色工具替换 | 权限范围不扩大；已有 shell 和模型绑定不被偷偷替换 |
| 通用压缩和原生压缩、重试与最终回答 | 辅助用量写正确的记账目标，不混入其他会话 |
| 新建、打开、回收、切回运行中 TUI 会话 | 保持原运行实例、迟到写入限制及一次激活事件 |

验证入口优先使用已有 Session、permission context、observability、subagent、team 及 TUI 会话切换场景。

## 交付范围

独立 PR，建议标题：`refactor: centralize runtime construction and session ownership`。

完成条件是内置主 Agent、队友和子 Agent 都经过同一创建入口，构造期间不替换活动 store，消息、记账和审查归属可从参数追踪，扩展迁移要求明确。JSONL 格式、工具角色范围和子任务生命周期保持兼容。
