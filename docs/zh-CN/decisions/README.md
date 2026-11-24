# 架构决策记录

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/decisions/README.md)

本目录记录 Virgo 中**已落地且对后续工作有实质影响的架构与工程决定**（包含关键取舍、约束及原因）。过程性讨论、临时里程碑和废案不写入此处。

| 编号 | 决策主题 | 确立的核心约束 |
| --- | --- | --- |
| [001](001-guardrail-threat-model.md) | 护栏而非堡垒的威胁模型 | 以 `<= 5 秒` 冷启动与 O(1) 工具延迟为前提约束良性模型的偶发误操作；确认 `.virgo` 写入与高危命令，而非追求对抗级隔离 |
| [002](002-execution-boundaries-and-broker-tools.md) | 分层执行边界与 Broker 直连文件工具 | Broker 文件工具直接按 `PermissionPolicy` 校验、仅 Shell 进入 OS 沙箱、可信集成运行在 Host、任务捕获不可变上下文、失败关闭且绝不重放 |
| [003](003-native-sandbox-backends.md) | 一级平台原生沙箱后端选型与平台取舍 | 采用各平台最简可靠的原生机制（`bubblewrap`、直接 Seatbelt + `HostProcess`、非提权 Windows 标准 AppContainer、WSL2），并如实记录平台差异 |
| [004](004-untrusted-project-config-isolation.md) | 不可信项目配置隔离与授权指纹绑定 | 禁止项目覆盖执行权限、合并前剥离不可信项目活跃配置、拒绝符号链接项目提示词文件，并将 MCP 授权绑定配置指纹 |
| [005](005-runtime-layering-and-durable-state.md) | 运行时分层、扩展机制边界与本地持久化存储 | 保持单向包依赖分层、区分核心工具 / 内置插件 / 延迟内置扩展、由 Core 统一托管后台任务，并隔离各持久化存储的故障域 |
