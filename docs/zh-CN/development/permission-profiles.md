# 权限模式与执行

| 模式 | 文件权限 | 公共网页工具 | Shell 联网 | 执行后端 |
| --- | --- | --- | --- | --- |
| Read-Only | Broker 文件工具默认以工作区为界（越界路径按前缀确认一次）；POSIX Shell 默认只读访问宿主常规路径（屏蔽高敏凭据路径），拥有独立临时目录与会话缓存 | 允许 | 按资源申请 | 原生沙箱 |
| Workspace-Edit | 可读写真实工作区（开放 `.git`、`AGENTS.md`、`CLAUDE.md` 与 `.venv`；修改项目 `.virgo` 时弹窗确认；高敏凭据路径硬性禁读写） | 允许 | 允许 | 原生沙箱 |
| No-Limit | 使用宿主账户权限 | 允许 | 允许 | Host |

## 默认模式与选择方式

- **受支持的一级平台（Linux、macOS、已启用 `null.sys` Feature `60692039` 的新版 Windows 11 与 WSL2）**：默认 **Workspace-Edit**。受支持的新版 Windows 11 使用非提权标准 AppContainer 后端（`windows-appcontainer`，无需管理员权限）；`\Device\Null` 缺少 `S-1-15-2-1` 的旧版 Windows 默认以 **No-Limit** 启动并显示一次性提示（**1. WSL2**、**2. No-Limit**、**3. 更新 Windows**），显式选择沙箱模式时 fail-closed（详见[原生 Windows 沙箱](../guides/windows-sandbox.md)与 [WSL2 使用指南](../guides/wsl2.md)）。
- 在用户设置中配置 `execution.mode`（`read-only`、`workspace-edit` 或 `no-limit`），或通过命令行 `--permission <mode>` 启动。已退役的配置项（`sandbox`、`execution.provider`、`permissions.preset`/`presets`/`mode`/`modes`/`allow`/`deny`）会直接报错并指引迁移至 `execution.mode`；已移除的旧命令行参数（`--execution`、`--mode`、`--sandbox`）则作为未识别参数被直接拒绝。
- 在 TUI 中，每按一次 Shift+Tab 就切换到下一个模式，顺序为 Read-Only → Workspace-Edit → No-Limit → Read-Only。底部状态栏显示选中的模式。任务执行期间切换时，状态栏同时显示当前任务和下一任务的模式。运行中的任务及其排队子任务沿用启动时的执行上下文。使用 `/permissions show` 或 `/permissions list` 查看模式，也可以用 `/permissions <id>` 直接指定模式。

## 资源审批、高危命令确认与持久授权

Core 依据 deny 优先的 `PermissionPolicy` 校验每次工具调用：

- **文件工具**（`read`、`write`、`edit`、`glob`、`grep`）由 Broker 直接按 `PermissionPolicy` 执行。越界读写与修改 `.virgo` 在派发前按需弹窗询问，解析符号链接展示真实路径，并在批准后当场放行执行（无需模型两阶段重发）。高敏凭据路径（`~/.virgo`、`~/.ssh`、`~/.aws` 等）硬性拒绝，不可授权。
- **Shell 命令**在 Read-Only 与 Workspace-Edit 下运行在 OS 沙箱中，可通过 `request_permissions`（`tool` 与 `arguments` 为可选字段）申请额外路径前缀或联网开关（`network_allowed: true`），支持与对应 Shell 工具在同一轮发出。当无法进行交互式提权审批时（No-Limit、`--approval never` 或非交互界面），`request_permissions` 会自动对模型隐藏。
- **高危 Shell 命令**（`git push --force`、推送至受保护分支、`git reset --hard`、`git clean -f`、`curl | sh`、工作区外递归删除、部署/发布、云资源删除，以及通过数据库/缓存 CLI 执行的破坏性 SQL/Redis 语句）在**所有模式**下均需逐次确认。配置 `permissions.confirmHighRisk: false` **仅在 No-Limit 模式下**关闭该确认。
- **沙箱不可用时的一次性回退**：当原生沙箱后端在启动载荷前无法建立时，Virgo 明确报错，并在交互式会话中提供显式的“本次使用 No-Limit”选项（不改变会话模式）。
- **允许（`y`）**按 `(read_paths | write_paths, 规范路径前缀)` 或 `network_allowed` 对**当前对话与模式**持续生效（调用 `/permissions revoke`、切换对话或切换模式时清空），不再绑定文件正文 JSON、`st_ino` 或 `os.environ` 哈希。选择拒绝（`n`）会在本轮提示词内阻止重复打扰，下一轮用户提问时正常重新询问。
- **始终允许（`a`）**按 `(project, mode, resource, target)` 保存在 `<VIRGO_HOME>/permission-grants.sqlite3`（`resource_grants` 表；升级时自动清理旧版 `grants` 表与 `resource-grants.sqlite3`）。用 `/permissions grants` 查看、用 `/permissions revoke ID` 撤销。项目配置无权添加授权。
- **受信任扩展与 MCP 工具**以 **Host** 权限运行。声明为只读的工具直接执行；可能修改宿主状态的工具在 Read-Only 与 Workspace-Edit 下需要审批（No-Limit 不询问）。MCP 工具支持“始终允许”，按 `f"{tool_name}:{config_fingerprint}"` 绑定服务器展开后的 `command`、`args`、`env` 的 SHA-256 指纹；服务器配置一旦变化，旧授权自动失效。
