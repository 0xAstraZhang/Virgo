# 权限模式与审批

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/guides/permissions.md)

| 模式 | 文件权限 | 公共网页工具 | Shell 联网 | 执行后端 |
| --- | --- | --- | --- | --- |
| Read-Only | Broker 文件工具默认以工作区为界（越界路径按需确认）。沙箱 Shell 在 POSIX 上只读访问宿主常规路径（屏蔽高敏凭据路径），在原生 Windows 上只读访问工作区及 AAP 可读的宿主/运行时/工具链路径，均提供可写的单命令临时目录与会话缓存。 | 允许 | 按资源申请 | 原生沙箱 |
| Workspace-Edit | 可读写真实工作区（开放 `.git`、`AGENTS.md`、`CLAUDE.md` 与 `.venv`；修改项目 `.virgo` 时弹窗确认；高敏凭据路径硬性禁读写）。 | 允许 | 允许 | 原生沙箱 |
| No-Limit | 使用宿主账户权限 | 允许 | 允许 | Host |

`web_fetch` 直连时收到 HTTP 429，表示目标网站对本次请求限流。DeepSeek 网页读取服务返回 HTTP 错误时，目标页面的状态仍未知。读取超时表示未收到完整页面。

## 默认模式与选择方式

- **受支持的一级平台（Linux、macOS、已更新的新版 Windows 11 与 WSL2）**：默认 **Workspace-Edit**。不支持非提权标准 AppContainer 的旧版 Windows 默认以 **No-Limit** 启动并显示一次性提示，显式选择沙箱模式时 fail-closed（详见[原生 Windows 沙箱](windows-sandbox.md)与 [WSL2 使用指南](wsl2.md)）。
- 在用户设置中配置 `execution.mode`（`read-only`、`workspace-edit` 或 `no-limit`），或通过命令行 `--permission <mode>` 启动。已退役的配置项（`sandbox`、`execution.provider`、`permissions.preset`/`presets`/`mode`/`modes`/`readOnlyCommands`/`allow`/`deny`）会直接报错并指引迁移，已移除的旧命令行参数（`--execution`、`--mode`、`--sandbox`）则作为未识别参数被拒绝。
- 在 TUI 中，`Shift+Tab` 按顺序循环切换模式（`Read-Only → Workspace-Edit → No-Limit → Read-Only`）。底部状态栏显示选中的模式；任务执行期间切换时，会同时显示当前任务和下一任务的模式。运行中的任务及其排队子任务沿用启动时捕获的执行上下文。使用 `/permissions list` 列出可选模式，或用 `/permissions <id>` 直接指定模式。
- 在交互式会话中，`/permissions` 汇总当前模式、审批策略、实际生效的可读写路径或宿主账户权限，以及网页访问和 Shell 联网状态（下一任务模式不同时单独列出）。使用 `/permissions show`、`/permissions status` 或非交互式的 `-p /permissions` 查看完整的执行边界 JSON。

## 资源审批、高危命令确认与持久授权

Core 依据 deny 优先的 `PermissionPolicy` 校验每次工具调用：

- **文件工具**（`read`、`write`、`edit`、`glob`、`grep`、`present_file`）由 Broker 直接按 `PermissionPolicy` 执行。越界路径与修改 `.virgo` 在派发前按需弹窗询问，解析符号链接以展示真实目标路径，批准后当场放行执行。`read`、`write`、`edit`、`present_file` 按解析后的精确文件路径授权；`glob`、`grep` 按请求的目录前缀授权。高敏凭据路径（`~/.virgo`、`~/.ssh`、`~/.aws` 等）硬性拒绝，不可授权。
- **Shell 命令**在 **Read-Only** 与 **Workspace-Edit** 下运行在 OS 沙箱中，可通过 `request_permissions`（`tool` 与 `arguments` 为可选字段）申请额外路径前缀或联网开关（`network_allowed: true`），支持与对应 Shell 工具在同一轮发出。当无法进行交互式提权审批时（**No-Limit**、`--approval never` 或非交互界面），`request_permissions` 会自动对模型隐藏。
- **高危 Shell 命令**（`git push --force`、推送至受保护分支、`git reset --hard`、`git clean -f`、`curl | sh`、工作区外递归删除、部署/发布、云资源删除，以及通过数据库/缓存 CLI 执行的破坏性 SQL/Redis 语句）在**所有模式**下均需逐次确认。用户明确选择 `n` 时返回 `user_declined`，并告知模型命令未启动、仅在用户要求时才可重试。无法交互确认、确认取消、确认失效和审批被禁用时分别返回对应原因。配置 `permissions.confirmHighRisk: false` **仅在 No-Limit 模式下**关闭该确认。
- **沙箱不可用时的一次性回退**：当原生沙箱后端在启动载荷前无法建立时，Virgo 明确报错，并在交互式会话中提供显式的“本次使用 No-Limit”选项（不改变会话模式）。
- **允许（`y`）**：按 `(read_paths | write_paths, 规范路径)` 或 `network_allowed` 在内存中为当前 `(对话, 模式)` 作用域授予临时权限。不同 `(对话, 模式)` 组合的授权彼此隔离且在切换时保留，调用 `/permissions revoke` 或 `/permissions ask|never` 时清空。
- **拒绝（`n`）**：返回 `reason: user_declined` 和 `escalation_available: false`。无法确认、请求取消或失效、审批被禁用时均不记录为用户拒绝。重复申请或申请被已拒绝范围涵盖的权限时，直接返回 `already_denied_this_turn` 而不再弹窗。路径按规范化后的目录层级比较：父目录涵盖子路径，写权限涵盖读权限。同级路径仍可申请，写权限被拒后仍可申请读权限。联网和外部工具的拒绝仅适用于相同授权。新一轮用户提问或执行 `/permissions revoke` 会清除同轮拒绝记录。
- **始终允许（`a`）**：按 `(project, mode, resource, target)` 保存在 `<VIRGO_HOME>/permission-grants.sqlite3`（`resource_grants` 表）。用 `/permissions grants` 查看、用 `/permissions revoke <id>` 撤销。项目配置无权添加授权。
- **受信任扩展与 MCP**：受信任扩展与 MCP 客户端以 **Host** 权限运行，工具由配置的 MCP 服务器执行。声明为只读的外部工具直接执行；可能修改状态的工具在 **Read-Only** 与 **Workspace-Edit** 下需要审批，在 **No-Limit** 下直接执行。MCP 的“始终允许”按 `f"{tool_name}:{config_fingerprint}"` 绑定配置指纹（见 [MCP 配置](extensions.md#mcpmcp)）。

另见：[执行边界与沙箱](sandbox.md) · [原生 Windows 沙箱](windows-sandbox.md) · [执行接口](../development/execution-interface.md)
