# 执行边界与沙箱

> Virgo 的沙箱是防止误操作的**护栏**：它把模型偶发失误的影响限制在当前工作区内，保护 Virgo 的私有数据和项目控制文件，并在越界或高危操作前请求确认。它**不是**抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行 Virgo。

在 Windows 上，推荐在 WSL2 中运行 Virgo，这样可以使用与 Linux 相同的沙箱。原生 Windows 沙箱是实验性功能，启动较慢，功能也有限。详见 [WSL2 使用指南](wsl2.md)和[原生 Windows 沙箱（实验性）](windows-sandbox.md)。

## 护栏覆盖的目标

1. **工作区范围与控制文件写保护**：**Workspace-Edit** 默认仅允许写入当前项目工作区。控制元数据（`.git`、`.virgo`、`AGENTS.md`、`CLAUDE.md`）与解释器安装目录受写保护。Broker 文件工具（`read`、`write`、`edit`、`glob`、`grep`）直接在应用层按 `PermissionPolicy` 校验（即使控制文件在启动时尚不存在，也会拒绝写入），并使用最后一级 `O_NOFOLLOW` 与原子临时文件替换。
2. **私有数据与凭据保护**：Linux 与 macOS 上 Virgo 的私有目录（`~/.virgo` 或 `$VIRGO_HOME`）默认拒绝读写（POSIX 上即使以 `$HOME` 作为工作区，也会对私有目录做 carve-out）。**Read-Only** 默认关闭 Shell 网络，**Workspace-Edit** 允许 Shell 联网。
3. **高危命令逐次确认**：高代价或不可逆的 Shell 命令（`git push --force`、推送至受保护分支、`git reset --hard`、`git clean -f`、`curl | sh`、工作区外递归删除、部署/发布、云资源删除以及破坏性 SQL/Redis 语句）在**所有模式**下均要求显式一次性确认（仅可在 No-Limit 模式下通过 `permissions.confirmHighRisk: false` 关闭）。
4. **失败可见，不悄悄降级**：原生沙箱后端无法建立时，Virgo 明确报错，在交互式会话中提供显式的“本次使用 No-Limit”选项（不改变会话模式），绝不静默退回 Host，也绝不自动重放可能已经启动的命令。

## 不在范围内的目标

- 抵御主动试图绕过护栏的模型。
- 抵御精心构造的攻击链（TOCTOU 竞态、预埋软硬链、大小写变形、恶意依赖、恶意并发宿主写入者或操作系统/内核漏洞）。
- 承诺防住提示词注入攻击链（但私有目录只读拒绝、控制文件写保护和 Read-Only 断网仍能缓解常见的凭据窃取、持久化与数据外传风险）。
- 要求所有操作系统的隔离强度完全一致。

## 各平台后端与如实说明的差异

- **Linux（一级 — `bubblewrap` + `libseccomp2`）**：需要系统 `bwrap`、`libseccomp.so.2` 与非特权用户命名空间。隔离挂载、网络命名空间（关闭联网时）及系统调用，同时放行匿名 Unix 流 `socketpair(AF_UNIX, SOCK_STREAM, 0)`，确保 Python `asyncio` 与测试框架正常运行。已存在的控制文件通过只读 bind 挂载保护；不存在的控制文件名由 Broker 文件工具与不可信项目配置剥离保护。
- **macOS（一级 — Seatbelt）**：使用 `/usr/bin/sandbox-exec` 搭配 launchd 监督器清理进程树。Seatbelt 对已存在和尚不存在的控制元路径均实施写拒绝。
- **Windows + WSL2（一级）**：Windows 用户的推荐方式，在 WSL2 中直接使用 Linux `bubblewrap` + `libseccomp2` 后端。
- **原生 Windows（实验性）**：功能冻结的 LPAC 后端，默认以 **No-Limit** 启动。见[原生 Windows 沙箱（实验性）](windows-sandbox.md)。

## Broker 工具、输出存储与可信集成

- **文件工具**由 Broker 直接按 `PermissionPolicy` 执行（不再启动独立的 OS 沙箱 helper 进程），保证冷启动在 5 秒内、单次 Read 开销不随工作区规模增长。
- **命令输出**通过管道写入 Broker 管理的会话级持久 `OutputStore`（位于 `<VIRGO_HOME>/outputs/session-<id>`），恢复历史会话后仍可通过 `read_output` 读取，随会话删除或 7 天 TTL 清理；未截断的前台命令不残留文件。
- **用户可信集成**（Python 扩展、命令 hooks、stdio MCP 服务器）以 **Host** 权限运行；信任边界在安装或显式信任项目（`extensions.trustedProjects` / `--trust-project-extensions`）。不可信项目中的 `hooks`、`mcpServers`、`.virgo/mcp.json`、`providers` 与 `extensions` 会在合并到用户配置前被剥离。

见[权限模式与审批](../development/permission-profiles.md) · [原生执行验收](../development/execution-acceptance.md) · [WSL2 使用指南](wsl2.md) · [原生 Windows 沙箱（实验性）](windows-sandbox.md)
