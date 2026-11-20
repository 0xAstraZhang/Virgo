# 权限模式与执行

| 模式 | 文件权限 | 公共网页工具 | Shell 联网 | 执行后端 |
| --- | --- | --- | --- | --- |
| Read-Only | 可读项目及所需运行时文件，命令有私有临时目录 | 允许 | 按操作申请 | 原生沙箱 |
| Workspace-Edit | 可读写真实工作区；`.git`、`.virgo`、`AGENTS.md`、`CLAUDE.md` 与解释器目录受写保护 | 允许 | 允许 | 原生沙箱 |
| No-Limit | 使用宿主账户权限 | 允许 | 允许 | Host |

## 默认模式与选择方式

- **Linux、macOS 与 WSL2**：默认 **Workspace-Edit**。
- **原生 Windows**：默认 **No-Limit**，因为原生 Windows LPAC 沙箱为实验性功能（详见 [WSL2 使用指南](../guides/wsl2.md) 与[原生 Windows 沙箱（实验性）](../guides/windows-sandbox.md)）。
- 在用户设置中配置 `execution.mode`（`read-only`、`workspace-edit` 或 `no-limit`），或通过命令行 `--permission <mode>` 启动。已退役的配置项（`sandbox`、`execution.provider`、`permissions.preset`/`presets`/`mode`/`modes`/`allow`/`deny`）与旧参数（`--execution`、`--mode`、`--sandbox`）会直接报错并指引改用 `execution.mode` / `--permission`。
- 会话内使用 `/permissions show`、`/permissions list`、`/permissions <id>` 或 TUI 的 Shift+Tab 查看和切换模式。切换模式作用于下一任务；运行中的任务及其排队子任务保持启动时固定的不可变执行上下文。

## 操作审批、高危命令确认与持久授权

Core 依据 deny 优先的 `PermissionPolicy` 校验每次工具调用：

- **文件工具**（`read`、`write`、`edit`、`glob`、`grep`）由 Broker 直接按 `PermissionPolicy` 执行。越界读写在派发前询问用户，并在审批时解析符号链接以展示真实路径。单文件授权直接生效，无需授权父目录。
- **Shell 命令**在 Read-Only 与 Workspace-Edit 下运行在 OS 沙箱中，可通过 `request_permissions` 申请已存在的额外路径或联网开关（`network_allowed: true`）。
- **高危 Shell 命令**（`git push --force`、推送至受保护分支、`git reset --hard`、`git clean -f`、`curl | sh`、工作区外递归删除、部署/发布、云资源删除、破坏性 SQL/Redis）在**所有模式**下均需逐次确认。配置 `permissions.confirmHighRisk: false` **仅在 No-Limit 模式下**关闭该确认。
- **沙箱不可用时的一次性回退**：当原生沙箱后端在启动载荷前无法建立时，Virgo 明确报错，并在交互式会话中提供显式的“本次使用 No-Limit”选项（不改变会话模式）。
- **仅允许一次**绑定当前任务的确切操作，派发时消费（`request_permissions` 显式申请的授权在 5 分钟后或本轮任务结束时失效）。
- **始终允许**保存在 `<VIRGO_HOME>/permission-grants.sqlite3`（升级启动时会自动删除旧版 `resource-grants.sqlite3`，不迁移旧数据）。用 `/permissions grants` 查看、用 `/permissions revoke ID` 撤销。项目配置无权添加授权。
- **受信任扩展与 MCP 工具**以 **Host** 权限运行。声明为只读的工具直接执行；可能修改宿主状态的工具在 Read-Only 与 Workspace-Edit 下需要审批（No-Limit 不询问）。MCP 工具支持“始终允许”，并与服务器展开后的 `command`、`args`、`env` 的 SHA-256 指纹绑定；服务器配置一旦变化，旧授权自动失效。
