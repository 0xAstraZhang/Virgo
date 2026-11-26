# Virgo 文档中心

[项目首页](../../README.zh-CN.md) · [English](../en/README.md)

文档按读者角色和主题分层组织，便于按需阅读。

## 使用指南

面向使用者的功能行为、平台配置与内置扩展：

- [权限模式与审批](guides/permissions.md) — 三种执行模式（`Read-Only`、`Workspace-Edit`、`No-Limit`）、按需审批、会话与持久授权、同轮拒绝约束及高危命令确认。
- [执行边界与沙箱](guides/sandbox.md) — 护栏威胁模型、凭据与 `.virgo` 保护、临时目录生命周期、各平台后端概览以及 Broker/Host 边界。
- [原生 Windows 沙箱](guides/windows-sandbox.md) — 新版 Windows 11 的标准 AppContainer 后端、运行条件、旧版回退策略及平台限制。
- [WSL2 使用指南](guides/wsl2.md) — 在 WSL2 中使用 Linux `bubblewrap` 后端运行 Virgo。
- [内置扩展（Todos 与 MCP）](guides/extensions.md) — 内置 `todo` 与 `mcp` 扩展、解析优先级、项目信任、传输协议、凭据脱敏与输出上限。

## 开发与架构

系统分层、内部契约与验收验证：

- [架构概览](development/architecture.md) — 包依赖分层、核心工具 / 内置插件 / 内置扩展的边界，以及本地持久化存储布局。
- [功能需求与公理设计分析](development/axiomatic-design.md) — FR0–FR9 概览、系统约束、静态 DSM 范围，以及宏观关系表的耦合性质。
- [叶级功能需求](development/functional-requirements.md) — 可观察结果、可调整设计机制与验收边界。
- [功能验收](development/functional-acceptance.md) — 需求到测试的映射、受控功能影响及证据边界。
- [执行接口](development/execution-interface.md) — 核心 `PermissionPolicy`、`SandboxExecutor`、`ProcessRequest`、原生后端编译、Broker 文件工具、`OutputStore` 与 Host MCP 契约。
- [原生执行与性能验收](development/execution-acceptance.md) — 跨平台原生隔离验收套件、CI 门禁与真实规模性能预算。

## 架构决策

[决策记录索引](decisions/README.md)汇总已落地的架构决定及其取舍与约束。

## 示例

- [项目定制示例](../../examples/README.zh-CN.md)
