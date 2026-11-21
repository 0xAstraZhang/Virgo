# 执行边界与沙箱

> Virgo 的沙箱是防止误操作的**护栏**：它把模型偶发失误的影响限制在当前工作区内，保护 Virgo 的私有数据和项目控制文件，并在越界或高危操作前请求确认。它**不是**抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行 Virgo。

Linux、macOS、受支持的新版 Windows 11（`null.sys` Feature `60692039`）与 Windows + WSL2 均为**一级支持**平台，默认以 **Workspace-Edit** 启动。受支持的 Windows 11 使用标准 AppContainer（非提权运行，无需管理员权限）；`\Device\Null` 缺少 `ALL APPLICATION PACKAGES`（`S-1-15-2-1`）的旧版 Windows 默认以 **No-Limit** 启动并提示 **1. WSL2**、**2. No-Limit** 或 **3. 更新 Windows**。WSL2 使用 Linux 沙箱后端，适合旧版 Windows、Linux 工具链或需要在沙箱命令中访问本机回环服务（`127.0.0.1`）的场景。详见[原生 Windows 沙箱](windows-sandbox.md)与 [WSL2 使用指南](wsl2.md)。

## 护栏覆盖的目标

1. **工作区范围与控制文件写保护**：**Workspace-Edit** 默认仅允许写入当前项目工作区。控制元数据（`.git`、`.virgo`、`AGENTS.md`、`CLAUDE.md`）与解释器安装目录受写保护。Broker 文件工具（`read`、`write`、`edit`、`glob`、`grep`）直接在应用层按 `PermissionPolicy` 校验（即使控制文件在启动时尚不存在，也会拒绝写入），并使用最后一级 `O_NOFOLLOW` 与原子临时文件替换。
2. **私有数据与凭据保护**：Linux、macOS 与原生 Windows 上 Virgo 的私有目录（`~/.virgo` 或 `$VIRGO_HOME`）默认拒绝读写（POSIX 上即使以 `$HOME` 作为工作区，也会对 `~/.virgo` 做 carve-out）。`~/.ssh` 等其他用户目录在工作区之外时拒绝读写（原生 Windows 上 `%USERPROFILE%` 默认不含 `ALL APPLICATION PACKAGES`，且不允许以 `$HOME` 作为工作区）。**Read-Only** 默认关闭 Shell 网络，**Workspace-Edit** 允许 Shell 出站联网。
3. **高危命令逐次确认**：高代价或不可逆的 Shell 命令（`git push --force`、推送至受保护分支、`git reset --hard`、`git clean -f`、`curl | sh`、工作区外递归删除、部署/发布、云资源删除以及破坏性 SQL/Redis 语句）在**所有模式**下均要求显式一次性确认（仅可在 No-Limit 模式下通过 `permissions.confirmHighRisk: false` 关闭）。
4. **失败可见，不悄悄降级**：原生沙箱后端无法建立时，Virgo 明确报错，在交互式会话中提供显式的“本次使用 No-Limit”选项（不改变会话模式），绝不静默退回 Host，也绝不自动重放可能已经启动的命令。

## 不在范围内的目标

- 抵御主动试图绕过护栏的模型。
- 抵御精心构造的攻击链（TOCTOU 竞态、预埋软硬链、大小写变形、恶意依赖、恶意并发宿主写入者或操作系统/内核漏洞）。
- 承诺防住提示词注入攻击链（但私有目录只读拒绝、控制文件写保护和 Read-Only 断网仍能缓解常见的凭据窃取、持久化与数据外传风险）。
- 要求所有操作系统的隔离强度完全一致。

## 各平台后端与如实说明的差异

- **Linux（一级 — `bubblewrap` + `libseccomp2`）**：需要系统 `bwrap`、`libseccomp.so.2` 与非特权用户命名空间。隔离挂载、网络命名空间（关闭联网时）及系统调用，同时放行匿名 Unix 流 `socketpair(AF_UNIX, SOCK_STREAM, 0)`，确保 Python `asyncio` 与测试框架正常运行。已存在的控制文件（以及 `CLAUDE.md -> AGENTS.md` 这类软链指向的可读目标）通过只读 bind 挂载保护；不存在的控制文件名及删除软链目录项本身属于 Linux bind 挂载的固有限制，Broker 文件工具与不可信项目配置剥离仍对其进行保护。
- **macOS（一级 — Seatbelt）**：直接在独立 POSIX 进程组中启动 `/usr/bin/sandbox-exec`（与 Linux `bwrap` 及 POSIX Host 命令一致，由 `HostProcess` 通过 `waitid(WNOWAIT)` / `kqueue` + `os.killpg` 清理同进程组子进程树）。Seatbelt 对已存在和尚不存在的控制元路径均实施写拒绝。显式调用 `setsid()` 或脱离进程组的后台进程不会被自动回收（与 POSIX Host 命令一致）。
- **原生 Windows（一级 — 标准 AppContainer `windows-appcontainer`）**：非提权运行（无需管理员权限，无 LPAC `0x2000F`、无 `WRITE_RESTRICTED`、不修改设备 DACL、无管理员互斥锁、不写入 Windows 防火墙规则），通过会话级单 AppContainer SID + 单次 OS 传播的 `SetNamedSecurityInfoW` 可继承 ACE、Job Object（`KILL_ON_JOB_CLOSE`）进程树清理、工作区祖先缺少 `S-1-15-2-1`（如 `C:\Users`）时的会话级 `DefineDosDeviceW` 虚拟盘符映射，以及带启动自动恢复的预写 JSON 日志（`%LOCALAPPDATA%\Virgo\sandbox-sessions\`）实现。已存在的控制文件通过 `protected_readonly`（`SE_DACL_PROTECTED` + 显式 `ALLOW RX`，因 Windows 忽略针对 `S-1-15-2-*` 的 `ACCESS_DENIED_ACE`）加工作区根目录省略 `FILE_DELETE_CHILD (0x40)` 防止写入、重命名与删除；尚不存在的控制文件名无法通过 NTFS 对象 ACL 阻止（与 Linux `bwrap` 一致），由 Broker 文件工具与 R5 配置剥离继续防护。私有用户目录（`~/.virgo`、`~/.ssh`）由默认 NTFS ACL 拒绝读写（不支持以 `$HOME` 作为工作区）。Read-Only（`capabilities=()`）阻断全部网络；Workspace-Edit（`internetClient` + `privateNetworkClientServer`）允许出站联网，**但本机回环（`127.0.0.1` / `[::1]`）在非管理员 AppContainer 下仍被阻止**（本地开发服务/数据库/代理请改用一次性 No-Limit 或 WSL2）。缺少 AAP 且无 `WRITE_DAC` 的管理员/SYSTEM 运行时目录会显式失败关闭（`windows_runtime_inaccessible`）。关于 CPython 3.13 `0o700`、`git init` 与 `SE_DACL_PROTECTED` 注意事项详见[原生 Windows 沙箱](windows-sandbox.md)。
- **Windows + WSL2（一级）**：在 WSL2 中直接使用 Linux `bubblewrap` + `libseccomp2` 后端，适合偏好 Linux 工具链或需要沙箱内访问本机回环服务的场景。见 [WSL2 使用指南](wsl2.md)。

## Broker 工具、输出存储与可信集成

- **文件工具**由 Broker 直接按 `PermissionPolicy` 执行（不再启动独立的 OS 沙箱 helper 进程），保证冷启动在 5 秒内、单次 Read 开销不随工作区规模增长。
- **命令输出**通过管道写入 Broker 管理的会话级持久 `OutputStore`（位于 `<VIRGO_HOME>/outputs/session-<id>`），恢复历史会话后仍可通过 `read_output` 读取，随会话删除或 7 天 TTL 清理；未截断的前台命令不残留文件。
- **用户可信集成**（Python 扩展、命令 hooks、stdio MCP 服务器）以 **Host** 权限运行；信任边界在安装或显式信任项目（`extensions.trustedProjects` / `--trust-project-extensions`）。不可信项目中的 `hooks`、`mcpServers`、`.virgo/mcp.json`、`providers` 与 `extensions` 会在合并到用户配置前被剥离。

见[权限模式与审批](../development/permission-profiles.md) · [原生执行验收](../development/execution-acceptance.md) · [原生 Windows 沙箱](windows-sandbox.md) · [WSL2 使用指南](wsl2.md)
