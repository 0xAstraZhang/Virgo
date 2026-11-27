# 功能需求与公理设计

[文档中心](../README.md) · [架构概览](architecture.md) · [English](../../en/development/axiomatic-design.md)

本页区分功能需求（FR）、设计参数（DP）与约束（C）。[叶级需求目录](functional-requirements.md)定义可观察的结果及实现选择；[功能验收](functional-acceptance.md)将这些结果和跨功能影响关联到可执行证据。

## 范围与定义

- **CA0 — 客户需求：** 提供轻量、可扩展的终端代理，在支持的平台上帮助用户探索和修改软件、执行命令并持续推进复杂任务。
- **FR0 — 总体功能：** 在遵守用户授权与资源限制、保留继续工作所需状态的条件下，协同完成软件工程任务。
- **DP0 — 总体实现：** 由 `AgentSession` 装配的分层编码代理运行时。

FR 说明**需要什么结果**；DP 说明**如何实现该结果，以及允许调整什么**。例如，恢复对话是 FR；带锁的追加式 JSONL 树是 DP。约束限定可接受方案的边界。权限执行本身是功能，其威胁模型、适用平台与开销上限是约束；不能仅凭“安全”或“性能”标签判定某项应属于 FR 还是约束。

概念设计关系为 $\{FR\}=[A]\{DP\}$。非零 $A_{ij}$ 表示：在给定变化范围和运行场景下，调整 $DP_j$ 会影响 $FR_i$ 所要求的结果。模块导入、回调或请求/响应往返是需要调查的线索，不自动等于这种功能影响。

## 产品约束与设计不变量

- **C1 — 护栏威胁模型：** 限制良性模型的偶发误操作；恶意代码隔离、蓄意逃逸与提示注入链防御不在此边界内（[ADR 001](../decisions/001-guardrail-threat-model.md)）。
- **C2 — 延迟与扩展性预算：** Tier 1 主机冷启动加首次 Shell `<= 5s`，重复短 Shell `<= 2s` 且保持 `O(1)` 扩展性，Broker `read` `<= 250ms` 且保持 `O(1)` 扩展性；未配置可选 SDK 时不产生其冷启动导入开销。这些是验收目标，不是依赖检查的推论（[执行验收](execution-acceptance.md)）。
- **C3 — 平台：** Linux、macOS、非提权且受支持的 Windows 11、Windows + WSL2，以及 Python 3.11+；原生保证需在对应主机验证（[ADR 003](../decisions/003-native-sandbox-backends.md)）。
- **C4 — 配置的工作上限：** 模型请求及 assistant 轮次应遵守配置的上限，包括适用的共享模型调用及 Web 调用上限。配置的上限是约束；计数、作用域与准入机制是 DP。为辅助工作另设无限预算会改变资源契约。

- **I1 — 执行正确性不变量：** 原生边界不可用时拒绝执行；交互式单次 No-Limit 选择不改变会话模式；payload 一旦可能启动就禁止自动重放（[ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)）。
- **I2 — 故障域不变量：** 插件加载、诊断报告与呈现失败应保留原操作结果和会话状态。作为权威记录的会话存储失败会有意停止后续工作，不能把它当作可忽略的日志失败（[ADR 005](../decisions/005-runtime-layering-and-durable-state.md)）。
- **I3 — 当前缓存设计的内部约束：** 普通轮次追加历史；系统指令与保留段落在两次压缩边界之间冻结（[ADR 006](../decisions/006-three-layer-provider-agnostic-compaction.md)）。

I1、I2 是必须保持的正确性契约，可以替换实现但不能放松契约。I3 则以当前缓存策略为条件：产品目标是延迟、成本和上下文连续性，而非特定前缀格式。不能将内部设计约束视为不可变的客户需求。

## 功能分解

总体目标 FR0 由选定的模型/工具循环运行时 DP0 实现。[叶级目录](functional-requirements.md)将 43 项可观察结果组织为八个功能域：A 用户交互、B 模型交互、C 操作与信息获取、D 授权与信任控制、E 任务编排与控制、F 会话与上下文管理、G 可观测性与诊断、H 能力扩展。

这是具有总体设计选择的功能目录，不宣称八个标题是独立的中间 FR–DP 对。若进一步建立严格的公理设计层级，须先明确每个父 FR 对应的 DP，再说明其如何导出子 FR（在功能域与设计域间交替分解，即 zigzagging）。不能从包目录推导这种层级。模型/工具编排描述协议行为，而非任意端到端任务的成功；后者在 FR0 层级验收。能力安装与所安装功能的结果分别描述。叶级目录是编号、功能结果、主要设计机制和验收边界的权威来源。

## 两种不同的矩阵

### 静态导入 DSM

[`test_architecture_matrix.py`](../../../tests/test_architecture_matrix.py)提取 AST 中的 `Import` 与 `ImportFrom`，包括顶层、函数局部和 `TYPE_CHECKING` 语句。它检查模块无环、运行时包导入无环与层级顺序、特定同级导入边界，以及特定跨边界访问模式。去掉自依赖后，将无环图拓扑排序即可得到严格下三角 DSM。

此范围不包含 `importlib.import_module` 等动态装配、回调、共享可变状态和请求预算竞争。例如，[`harness/extensions.py`](../../../src/virgo/harness/extensions.py)会动态加载配置的插件。DSM 门禁检查实现结构，既不覆盖全部运行时依赖，也不证明 FR 独立性。

### 稀疏叶级 FR–DP 影响矩阵

使用目录中的 43 个叶级编号作为行列标签。每对同编号 FR–DP 标记为 `X`（主要设计关系，不是实测偏导数）；下表是已调查的非对角元素的稀疏表示。表中记录现有测试断言的可观察结果，不证明数值敏感性矩阵，也不宣称某个提交已经通过测试。设置变化、契约内替换或故障注入均限定于所述场景。区分获准结果的变化（例如在资源上限处停止）与需求协议违约。

- `N`：有证据支持的非零影响，由已有约束或功能契约所要求。
- `U`：有证据支持的非零不必要影响，违反或不必要地限制其他需求。本表的受控检查尚未确立此类元素。
- `0*`：所述替换或故障场景下的限定不变性，不是普遍的零。
- `?`：未知。未列出的所有非对角元素均为未知；可能的机制或单纯的数据/控制交互不证明功能影响。

具体测试场景及断言的适用边界见[功能验收](functional-acceptance.md)。

| 受影响 FR | 变化的 DP | 元素 | 限定变化 / 结果 | 现有测试 |
| --- | --- | :---: | --- | --- |
| FR-E.1 | DP-E.4 | N | 固定低压缩阈值，改变调用上限影响下一次普通请求能否分派。 | `test_auto_compaction_and_task_share_request_budget` |
| FR-E.1 | DP-F.4 | N | 固定调用上限为 2，改变压缩阈值影响剩余调用用于任务还是摘要。 | `test_auto_compaction_and_task_share_request_budget` |
| FR-F.4 | DP-E.4 | N | 固定需要摘要的条件，改变调用上限影响是否生成摘要检查点。 | `test_auto_compaction_and_task_share_request_budget` |
| FR-E.1 | DP-F.1 | N | 注入权威历史写入失败，阻止后续请求并保留未保存历史（故障场景）。 | `test_failed_save_stops_requests_and_keeps_history` |
| FR-F.3 | DP-D.1 | N | 切换并恢复权限模式时，同一请求上下文中的写入边界随之变化。 | `test_permission_switch_updates_requests_and_restores_the_original_prefix` |
| FR-H.1 | DP-D.6 | N | 同一项目扩展仅在外部显式选择信任时执行。 | `test_project_extensions_require_trust_outside_the_project` |
| FR-D.3 | DP-A.4 | 0* | 固定请求、工作区、策略与 y/n 回答，将记录式输入适配器替换为 TerminalUI。 | `test_direct_file_approval_persists_in_session_and_preserves_the_mode` |
| FR-G.5 | DP-H.1 | 0* | 替换已注册且声明的诊断查询工具名称，新引用仍可正确使用。 | `test_renamed_query_reference_can_be_followed_by_the_model` |
| FR-G.6 | DP-H.1 | 0* | 替换已声明查询工具名称，待投递及检查点引用使用新名称。 | `test_pending_evidence_uses_the_declared_query_name`、`test_compaction_checkpoint_uses_the_declared_query_name` |
| FR-G.4 | DP-H.1 | 0* | 禁用查询插件，诊断日志仍然可用。 | `test_model_diagnostics_feature_can_be_disabled_without_disabling_logs` |
| FR-G.4 | DP-A.1 | 0* | 注入诊断 UI 打印失败，已持久化原错误的身份和去重保持有效。 | `test_failing_ui_does_not_destroy_original_error_or_recurse` |

重排序时，将非零非对角元素从对应 DP 节点指向受影响的 FR 节点。`N` 和 `U` 均为边；不能为了得到对角矩阵而去掉必要影响。上述六项有依据的非零元素构成 E.4 → E.1、F.4 → E.1、E.4 → F.4、F.1 → E.1、D.1 → F.3、D.6 → H.1 六条边。其余元素不加入**已知边图**，而非在完整矩阵中假定为零。

已知边图无有向环（所有强连通分量均为单节点）。保持主配对不变，一种有效的 FR/DP 同步重排序是：按目录顺序列出除 E.1、F.3、F.4、H.1 外的全部叶级编号，然后依次列出 F.3、F.4、H.1、E.1。此时已知非零结构为下三角、非对角结构。这不证明真实的 43×43 矩阵为三角矩阵：未知元素可能引入环，主要设计机制也可能共享设计选择。限定不变性不能将未知元素补成零。有意改变获准结果也不意味着对应 FR 违约。

粗粒度功能域聚合会混合同域中的不同机制，可能产生表观双向关系，例如共享预算下的任务推进与压缩。它不能替代叶级受控变化，因此不再为功能域构造功能矩阵。保留有真实需求的共享预算、显式授权、来源绑定和权威存储 fail-closed。先按具体功能验收边界调查未知或不必要影响，再决定是否修改实现；不得仅为消除矩阵元素引入抽象、独立预算或额外存储。

## 独立公理与信息公理

对角 FR–DP 矩阵是 **uncoupled（无耦合）**。可重排为三角形的矩阵是 **decoupled（可解耦）**，需要按相应顺序确定 DP。工具的执行顺序或不存在循环导入，都不能推出这两种性质。功能独立性关注结果，而非组件在物理上是否分离。

信息公理比较满足规定功能范围的概率：$I=-\log_2 P(\text{所有要求的 FR 范围同时满足})$。代码量、接口数量和矩阵非零项数量都不是这个信息量；未经独立性论证，也不能将各项边际成功率直接相乘。现有结构检查和确定性行为测试不估计该概率，也不构成公理设计的量化得分。

参考：[MIT 公理设计研究组定义](https://web.mit.edu/axiom/www/introduction.shtml)、[Suh 的公理设计导论](https://web.mit.edu/2.882/www/chapter1/chapter1.htm)、[MIT 关于叶级分解与重排的研究](https://web.mit.edu/pccs/pub/2003/melvin-manufacturing.pdf)。

另见：[叶级需求](functional-requirements.md) · [功能验收](functional-acceptance.md) · [架构概览](architecture.md)。
