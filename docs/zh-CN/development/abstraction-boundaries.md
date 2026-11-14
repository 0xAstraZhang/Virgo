# 抽象边界与维护规则

[架构总览](architecture.md) · [扩展指南](extensions.md) · [测试取舍](test-retirements.md) · [English](../../en/development/abstraction-boundaries.md)

AI、Agent、Harness/Plugins、CLI 分层分别承担传输、执行与应用策略。共享规则只有一个语义归属，调用方使用公共入口，避免复制策略和 Runtime 身份检查。

## 共享规则的归属

| 规则 | 维护位置 | 调用方与边界 |
| --- | --- | --- |
| OpenAI 推理强度映射 | `ai/providers/reasoning.py` | Chat、Responses、Codex 请求及 `completion_request_metadata()` 共用纯函数；显式映射、模型声明与默认回退保留原优先级。HTTP 客户端、认证和流事件仍由适配器维护。 |
| 权限模式词汇、显示名称与计划模式工具筛选 | `harness/permissions.py` | Harness 和 CLI 依赖共享契约；`harness/permission_policy.py` 实现必要规则、人工审批回退、缓存和审计；`plugins/auto_review.py` 提供可选模型审查；显示名称可传入会话翻译器。 |
| 工具上下文对应的可信 Runtime | `AgentSession.runtime_for_context()` | 诊断与 MCP 使用同一身份判定；具体功能负责处理拒绝和执行后续操作。解析不查询当前 UI 选择，不授予新的诊断访问权。 |

推理强度映射只共享真正一致的语义：Chat 兼容接口支持 `reasoningEffortMap`，Responses/Codex 保留各自的 `max → xhigh` 回退。不要用一个带大量分支的通用 Provider 对象掩盖协议差异，也不要在诊断记录里另写一套请求策略。

Runtime 解析要求来源是 `ReviewSource`，来源中的对象是本 Session 的 `Runtime`，来源对象与 Runtime 原始绑定相同，且工具上下文中的 Agent 名称一致。任一不满足就返回 `None`；不要把缺失上下文补成 `session.main`。

## 保留的结构

- `Agent` 维护消息、队列、取消和订阅；`run_loop()` 维护模型与工具的执行次序。工具错误、实际执行事实和最终结果投影与该生命周期紧密相关，继续放在循环附近。
- `Runtime` 维护单个对话的请求策略；`AgentSession` 负责创建、切换、服务和存储归属。候选构造与激活分开，避免失败时改写活动会话。
- CLI 的控制器、线程桥接、Transcript 与 Textual App 已分别承担调度、跨线程消息、展示和交互职责。App 较大，但按文件长度拆分会跨越会话面板和模态交互的生命周期。
- Provider 按协议转换请求和事件；具体工具按文件系统、Shell、输出预算与平台进程行为分工。已有适配器复用和兼容入口有实际用途，不为消除所有延迟导入而另建框架。
- 测试保留独立失败模式和真实边界；重复的浅层断言只有在现有更完整场景覆盖后才删除。CI 继续显示删除项并核验保留场景。

## 公开插件契约

### 自定义权限策略的能力范围

`PermissionPolicy` 提供普通工具的 `preflight()` 和 `before_execute()` 两个检查点。`api.register_permission_policy()` 将附加限制与必要 Core 策略组合。托管网页操作使用 `api.authorize_tool`，子运行时通过 `api.create_runtime(parent=..., permission=...)` 创建。官方插件不替换 Core 权限门，也不直接修改其他 Runtime 的模式。

范围判断和 `ReviewSource` 位于 Harness。可选模型审批被禁用或失败后，不确定操作仍回退到人工审批。未知调用方明确失败，不能借用当前界面中的对话身份。

### MCP 工具注册表发布

`api.replace_tools(namespace, entries)` 验证工具归属和完整候选集后原子发布。Runtime 只在下一次请求边界更新 Schema，正在执行的批次保留原工具对象。MCP 先验证候选连接，不重放业务调用；被替换的传输代次可以保守拒绝旧句柄。候选失败必须保留健康活动连接。

### 注册与生命周期

保留现有 `setup(api)` 与事件总线。Core 记录注册归属、拒绝冲突、回滚可选插件的部分初始化，并逆序执行带诊断的清理回调。Python 插件仍是可信的进程内代码；项目信任仅由用户配置或显式启动参数授予。不将这些契约扩展成包管理器、依赖求解器或热更新系统。

具体接口见 [Core／插件边界](core-plugin-boundaries.md)和[公开 API](extensions.md)。

## 现有验证入口

请求映射由 `test_credentials.py`、`test_codex.py`、`test_responses.py`、`test_auxiliary_requests.py` 验证；权限兼容由 CLI 参数、TUI 模式切换、计划模式及执行前复核场景验证；Runtime 身份由 `test_diagnostic_access.py`、`test_mcp_recovery.py` 的缺失、外来、伪造来源与会话切换场景验证。这些既有产品场景继续保留。本次 Core／插件改动在生产实现前加入 `test_core_plugins_e2e.py`，覆盖装配、信任、归属、清理、预算和迁移的新契约。
