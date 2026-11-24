# 002 — 分层执行边界与 Broker 直连文件工具

[决策索引](README.md) · [文档中心](../README.md) · [English](../../en/decisions/002-execution-boundaries-and-broker-tools.md)

**状态：** 已落地

## 背景

如果把所有工具（包括简单文件读取、用户信任的 Python 扩展和 MCP 服务器）都塞进 OS 沙箱，会引入辅助进程启动开销、破坏审批交互体验，并与本地工具链产生脆弱耦合；反之，如果放任模型生成的任意 Shell 命令直接在宿主执行，则无法守住工作区和宿主凭据边界。

## 决策

- **单一 deny 优先策略模型**：`virgo.execution_policy.PermissionPolicy` 是 Broker 文件工具与原生 Shell 沙箱共用的唯一不可变策略模型，覆盖三种显式模式（`read-only`、`workspace-edit`、`no-limit`）。已退役的旧策略配置键与 CLI 参数直接报错，不做静默迁移。
- **Broker 直连文件工具**：内置文件工具（`virgo.tools.fs` + `virgo.file_access` 中的 `read`、`write`、`edit`、`glob`、`grep`）直接在 Broker 内按 `PermissionPolicy` 校验，配合末级组件 `O_NOFOLLOW` 和临时文件原子 `os.replace`，不引入独立的 OS 沙箱辅助进程。
- **仅 Shell 进入 OS 沙箱**：模型派生的任意命令统一经 `SandboxExecutor.run_process(ProcessRequest, context, cancellation)`（`virgo.execution`）执行，在 **Read-Only** 与 **Workspace-Edit** 下编译到平台原生沙箱。
- **可信集成运行在 Host**：受信任的 Python 扩展、命令 hooks 及 MCP 客户端/stdio 服务器（`HostContext.unrestricted`）运行在 Host 上，信任边界在安装或显式信任项目时建立。
- **任务级不可变上下文捕获**：每次任务启动时捕获原始 `HostContext` 或 `SandboxContext` 以及 `ExecutionOrigin`；子 Agent 与后台任务始终沿用该快照，不受后续切换模式、分支或 UI 会话的影响。
- **失败关闭、不静默降级、不自动重放**：原生沙箱在载荷启动前无法建立时直接失败关闭（交互式 Shell 会话允许用户显式选择一次性 **No-Limit**，且不改变会话模式）；载荷释放后的任何失败（`ExecutionFailed`）绝不自动重放。

## 取舍与约束

- **常数级文件工具延迟**：省去文件工具沙箱辅助进程使 Broker `read` 在不同仓库规模下保持 O(1) 延迟，也让越界文件审批在解析符号链接并获批后即可当场执行。
- **执行层不可破坏的不变量**：修改 `virgo.execution` 或 `virgo.sandbox` 时，必须保持启动确认、取消、进程树清理和禁止自动重放语义。
- **硬拒绝不可提权**：高敏凭据路径不能通过 `request_permissions` 或持久授权放行。

## 相关文档

- [执行接口](../development/execution-interface.md)
- [权限模式与审批](../guides/permissions.md)
- [执行边界与沙箱](../guides/sandbox.md)
