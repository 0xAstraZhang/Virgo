# 原生 Windows 命令沙箱（实验性）

> **状态：实验性（功能冻结）。** 在 Windows 上，推荐在 **WSL2** 中运行 Virgo，以获得一级支持的 Linux 沙箱（`bubblewrap` + `libseccomp2`）。详见 [WSL2 使用指南](wsl2.md)。

受以下限制影响，**原生 Windows 默认使用 No-Limit**（`execution.mode = "no-limit"`）。启动时 Virgo 会提示推荐使用 WSL2；在原生 Windows 上手动选择 Read-Only 或 Workspace-Edit 时，会显示明确的实验性标识及限制说明。

## 当前 LPAC 后端的已知限制

1. **需要管理员权限**：现有 LPAC 实现需要修改设备 DACL（`\Device\KsecDD`）并创建属主为 Administrators 的协调互斥锁。在普通非管理员 Windows 11 桌面账户上，操作系统会返回 `WinError 1307`。Virgo 会在启动沙箱前做快速预检并给出切换到 WSL2 或 No-Limit 的明确指引，不再抛出裸露的 `WinError 1307`。
2. **每条命令约 14 秒额外开销**：每条命令都会新建随机 LPAC 配置和 SID，并对运行时文件（CPython、`.venv`、PowerShell）逐文件施加和撤销临时 ACL。CI 性能门槛不适用于原生 Windows。
3. **不强制私有目录读拒绝**：每次启动遍历 `~/.virgo` 修改 ACL 的逻辑已删除（该操作会与 OAuth 刷新和设置写入冲突导致 `WinError 5`）。请勿依赖实验性原生 Windows 沙箱阻止 Shell 命令读取 `~/.virgo`。
4. **不支持 `$HOME` 作为工作区**：在原生 Windows 沙箱模式下以 `$HOME`（或包含 `~/.virgo` 的目录）作为工作区会被拒绝，并提示使用 WSL2 或 No-Limit。
5. **非阻塞 CI**：非沙箱测试在单个 `windows-latest` runner 上作为阻塞门槛运行；`tests/harness/test_windows_sandbox_e2e.py` 改为每日定时运行（`windows-sandbox.yml`），不阻塞 PR 合并。

## 运行条件（仅当主动启用实验性沙箱模式时）

- 本地 NTFS 上的原生 x64 Python 3.11+ 与 PowerShell 7
- 具备创建 AppContainer 配置、修改设备 DACL 与临时文件 ACL 权限的管理员账户
- 宿主防火墙与 AppContainer 网络隔离服务处于运行状态（Workspace-Edit 请求联网能力，Read-Only 默认关闭）

## 回到一级支持的条件

现有 LPAC 代码已冻结，仅修复阻断性缺陷，不再新增能力或局部重构。原生 Windows 回到一级支持（并恢复默认 Workspace-Edit）需要改用**标准 AppContainer**（保留 `ALL APPLICATION PACKAGES`、不修改设备 DACL，让普通非管理员用户直接运行）加**会话级 SID** 与可继承 ACE 重写，并在非管理员 Windows 11 桌面账户上验证冷启动不超过 5 秒、单条命令开销不随工作区大小增长。
