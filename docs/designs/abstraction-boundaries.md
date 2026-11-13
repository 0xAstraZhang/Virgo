# 抽象边界与维护规则

[架构总览](../ARCHITECTURE.md) · [扩展迁移](../EXTENSION_MIGRATION.md) · [测试取舍](../testing/test-retirements.md)

本次审查以 `0ffc237` 为基线。总体的 AI、Agent、Harness/Features、CLI 分层成立；调整集中在规则的唯一归属与重复的调用方身份检查。

## 共享规则的归属

| 规则 | 维护位置 | 调用方与边界 |
| --- | --- | --- |
| OpenAI 推理强度映射 | `ai/providers/reasoning.py` | Chat、Responses、Codex 请求及 `completion_request_metadata()` 共用纯函数；显式映射、模型声明与默认回退保留原优先级。HTTP 客户端、认证和流事件仍由适配器维护。 |
| 权限模式词汇与计划模式工具筛选 | `harness/permissions.py` | Harness 和 CLI 依赖共享契约；`features/permission.py` 实现实际审批、规则、缓存和审计，兼容导出旧公开名称。 |
| 工具上下文对应的可信 Runtime | `AgentSession.runtime_for_context()` | 诊断与 MCP 使用同一身份判定；具体功能负责处理拒绝和执行后续操作。解析不查询当前 UI 选择，不授予新的诊断访问权。 |

推理强度映射只共享真正一致的语义：Chat 兼容接口支持 `reasoningEffortMap`，Responses/Codex 保留各自的 `max → xhigh` 回退。不要用一个带大量分支的通用 Provider 对象掩盖协议差异，也不要在诊断记录里另写一套请求策略。

Runtime 解析要求来源是 `ReviewSource`，来源中的对象是本 Session 的 `Runtime`，来源对象与 Runtime 原始绑定相同，且工具上下文中的 Agent 名称一致。任一不满足就返回 `None`；不要把缺失上下文补成 `session.main`。

## 保留的结构

- `Agent` 维护消息、队列、取消和订阅；`run_loop()` 维护模型与工具的执行次序。工具错误、实际执行事实和最终结果投影与该生命周期紧密相关，继续放在循环附近。
- `Runtime` 维护单个对话的请求策略；`AgentSession` 负责创建、切换、服务和存储归属。候选构造与激活分开，避免失败时改写活动会话。
- CLI 的控制器、线程桥接、Transcript 与 Textual App 已分别承担调度、跨线程消息、展示和交互职责。App 较大，但按文件长度拆分会跨越会话面板和模态交互的生命周期。
- Provider 按协议转换请求和事件；具体工具按文件系统、Shell、输出预算与平台进程行为分工。已有适配器复用和兼容入口有实际用途，不为消除所有延迟导入而另建框架。
- 测试保留独立失败模式和真实边界；重复的浅层断言只有在现有更完整场景覆盖后才删除。CI 继续显示删除项并核验保留场景。

## 后续需要单独设计的接口

### 自定义权限策略的能力范围

`PermissionPolicy` 声明 `preflight()` 和 `before_execute()`，这覆盖普通工具调用的两个检查点。部分内置功能还依赖更丰富的服务：`WebAccess._permitted()` 使用 `on_tool_call`、`mode_for()`、`decide()`；子 Agent 和队友的模式管理使用 `set_agent_mode()`。只实现最小协议的策略不能被假定为与这些功能兼容。

现有扩展建议包装并转发内置策略；相关说明见[权限策略迁移](../EXTENSION_MIGRATION.md#权限策略)。若需要正式支持独立最小策略，应先决定这些额外能力的显式契约或拒绝行为，再以 hosted web、子 Agent、队友场景在实现前验证。此次重构不改变权限决策或扩展能力范围。

### MCP 工具注册表发布

`MCPConnections._publish()` 仍直接更新 Session 的工具注册项。若继续增加动态能力，应考虑 Session 所有的批量替换入口，同时明确名称冲突、旧工具身份、并发调用和候选失败的原子性。只把元组操作搬进一个通用 setter 无法解决这些边界，因此本次保留原实现。

## 现有验证入口

请求映射由 `test_credentials.py`、`test_codex.py`、`test_responses.py`、`test_auxiliary_requests.py` 验证；权限兼容由 CLI 参数、TUI 模式切换、计划模式及执行前复核场景验证；Runtime 身份由 `test_diagnostic_access.py`、`test_mcp_recovery.py` 的缺失、外来、伪造来源与会话切换场景验证。这些产品测试已存在，本次未新增产品测试。
