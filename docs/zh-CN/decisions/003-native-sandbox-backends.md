# 003 — 一级平台原生沙箱后端选型与平台取舍

[决策索引](README.md) · [文档中心](../README.md) · [English](../../en/decisions/003-native-sandbox-backends.md)

**状态：** 已落地

## 背景

Linux、macOS 与 Windows 提供的进程和文件系统隔离原语存在本质差异。强行在三个操作系统间追求完全一致的内核级语义，往往需要管理员权限、后台守护进程、系统调用过滤或逐文件 ACL 遍历，既违背冷启动性能预算，又容易破坏常规开发工具链。

## 决策

Virgo 在四个**一级支持**平台上均选用最简可靠的原生机制，并如实记录平台差异：

- **Linux（`bubblewrap`）**：使用 `bwrap --unshare-all --unshare-user --disable-userns --cap-drop ALL`，通过 `--info-fd` 完成启动确认且不依赖 `libseccomp`。以 `--ro-bind / /` 挂载只读宿主和真实 `$HOME` 使各类开发工具链、`AF_UNIX`、`AF_NETLINK` 和 `io_uring` 正常工作，仅遮蔽高敏凭据路径（`--perms 000 --tmpfs` 或空只读文件）并对工作区、会话缓存和单命令 `TMPDIR` 挂载可写层。
- **macOS（Seatbelt + `HostProcess`）**：每个执行器启动时探测一次 `/usr/bin/sandbox-exec`，直接在独立 POSIX 进程组中拉起子进程（`HostProcess` 通过 `waitid(WNOWAIT)` / `kqueue` + `os.killpg` 回收），不维护后台 launchd 托管进程。放行只读宿主访问与工作区/缓存/`TMPDIR` 写入，硬拒绝私有凭据路径，并对 `.virgo`（无论是否已存在）实施写拒绝。
- **原生 Windows 11（非提权标准 AppContainer `windows-appcontainer`）**：无需管理员权限，以 `null.sys` Feature `60692039`（`\Device\Null` 授予 `ALL APPLICATION PACKAGES` `S-1-15-2-1`）为前提。同一 `(workspace, mode)` 会话复用单个 AppContainer 配置与 SID，通过单次 `SetNamedSecurityInfoW` OS 传播可继承 ACE，对已存在且未获批的 `.virgo` 调用 `protect_readonly_path()`（`SE_DACL_PROTECTED` + 显式 `ALLOW RX`），自动为 `.venv` 底层 Python 及用户自有 `PATH` 工具链授予只读 ACE，在祖先目录缺少 `S-1-15-2-1` 时映射会话内 `DefineDosDeviceW` 虚拟盘符，在 `%LOCALAPPDATA%\Virgo\sandbox-sessions\` 维护原子预写 JSON 租约日志用于启动崩溃恢复，并直接通过 `CreateProcessW` + 关闭即终止的 Job Object（`WindowsProcess`）托管子进程。
- **Windows + WSL2**：在 WSL2 中直接使用 Linux `bubblewrap` 后端，与原生 Windows 11 并列为一级支持路径。

## 取舍与约束

- **旧版 Windows 显式降级**：Virgo 生产代码绝不修改 `\Device\Null`。未启用 Feature `60692039` 的旧版 Windows 10/11 与默认 Windows Server 以 **No-Limit** 启动并给出一次性提示（WSL2 / No-Limit / 更新 Windows），显式指定沙箱模式时 fail-closed（`windows_null_device_unavailable`）。
- **接受真实平台限制，不通过提权绕过**：
  - 原生 Windows 非管理员 AppContainer 阻止本机回环（`127.0.0.1` / `[::1]`），需要本地服务/代理时使用一次性 **No-Limit** 或 WSL2；同时禁止将 `$HOME` 作为工作区。
  - Linux bind 挂载与 Windows NTFS/ReFS ACL 无法在可写工作区的 Shell 内阻止新建原本不存在的 `.virgo`（由 Broker 文件工具与不可信项目配置剥离兜底）。
  - macOS 下显式调用 `setsid()` 脱离进程组的后代进程不会被自动回收，与 POSIX Host 行为一致。
- **真实主机验收为前提**：每个一级后端都必须在真实主机上以 `VIRGO_REQUIRE_SANDBOX_TESTS=1` 零跳过通过原生验收套件（见[原生执行与性能验收](../development/execution-acceptance.md)）。

## 相关文档

- [执行边界与沙箱](../guides/sandbox.md)
- [原生 Windows 沙箱](../guides/windows-sandbox.md)
- [WSL2 使用指南](../guides/wsl2.md)
- [原生执行与性能验收](../development/execution-acceptance.md)
