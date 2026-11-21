# 原生 Windows 命令沙箱

> **状态：在已更新的新版 Windows 11（`null.sys` Feature `60692039`）上为一级支持（`windows-appcontainer`）。** 当系统 `\Device\Null` 已原生授予 `ALL APPLICATION PACKAGES`（`S-1-15-2-1`）读写权限（`null.sys 10.0.26100.8972+` 且启用 WIL Feature `60692039`）时，Virgo 默认启用 **Workspace-Edit**（`execution.mode = "workspace-edit"`），**无需管理员权限**即可非提权运行，适用冷启动（`<= 5 秒`）与 O(1) 单条命令性能门槛，并纳入 PR 阻塞验收 CI。在 `\Device\Null` 缺少 `S-1-15-2-1` 的旧版 Windows 10、旧版 Windows 11 与默认 Windows Server 2022/2025 上，Virgo 默认以 **No-Limit** 启动并给出一次性提示（**1. WSL2**、**2. No-Limit**、**3. 更新 Windows**）；若显式指定沙箱模式会 fail-closed 返回 `windows_null_device_unavailable`。如果你使用旧版 Windows、偏好 Linux 工具链或需要在沙箱命令中访问本机回环服务（`127.0.0.1`），[WSL2](wsl2.md) 同样是一级支持方案。

> Virgo 的沙箱是防止模型偶发误操作的**护栏**，不是抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行。详见[执行边界与沙箱](sandbox.md)。

## 标准 AppContainer 后端工作原理

Virgo 使用非提权**标准 AppContainer**（`src/virgo/sandbox/windows_sandbox.py`、`windows_acl.py`、`windows_files.py`）：

1. **直接启动非提权标准 AppContainer**：直接通过 `CreateProcessW`（`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` + `HANDLE_LIST` + `JOB_LIST`）启动子进程，保留 `ALL APPLICATION PACKAGES`（`S-1-15-2-1`），无需热路径 `CREATE_SUSPENDED` 挂起令牌重验开销。不启用 LPAC 退出标志（`0x2000F`），不使用 `CreateRestrictedToken` / `WRITE_RESTRICTED`，不修改 `\Device\KsecDD` 或 `\Device\Null` 设备 DACL，不创建 `O:BAG:BA` 管理员互斥锁，不修改 WindowStation/Desktop DACL，也不写入 Windows 防火墙规则。
2. **会话级单 SID + 用户工具链自动只读授权**：同一 `(workspace, mode)` 会话复用单个 AppContainer 配置与 SID，通过单次 `SetNamedSecurityInfoW` 由操作系统传播可继承 ACE（无 Python 逐文件 ACL 遍历或硬链接扫描），将冷启动控制在 5 秒内、重复命令保持 O(1) 启动开销。自动识别 `workspace/.venv/pyvenv.cfg`（`venv/pyvenv.cfg`）中的底层 Python 以及 `PATH` 中的用户自有（具备 `WRITE_DAC`、排除私有凭据目录）工具链根目录（`~/.cargo`、`~/.rustup`、`~/.local`、`scoop`、`go`、`%LOCALAPPDATA%\Programs\<Tool>`、`%LOCALAPPDATA%\uv`、`%LOCALAPPDATA%\Microsoft\WinGet` 等），授予会话级只读 ACE 并记入崩溃恢复日志。每条命令拥有独立的 `TMPDIR`（位于 `<AC>\Temp` 下），在 Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）排空进程树后立即清理。
3. **`.virgo` 控制目录保护（VER-01）**：**Workspace-Edit** 下开放 `.git`、`AGENTS.md`、`CLAUDE.md` 与 `.venv` 以支持日常开发流，仅对已存在且未获批的 `.virgo` 采用 `protected_readonly`（设置 `SE_DACL_PROTECTED`，保留全部非会话宿主 ACE 以确保宿主工具与编辑器原子保存不出现 `WinError 5`，并将本会话 SID 限制为显式 `ALLOW (RX) 0x001200A9`——不使用 `ACCESS_DENIED_ACE`，因为 Windows 内核在 AppContainer 检查中会忽略针对 `S-1-15-2-*` 的拒绝项）。若用户在会话中途批准写入 `.virgo`，会自动恢复可写继承。
4. **兼容 NTFS、ReFS Dev Drive、OneDrive 与祖先虚拟盘符（`DefineDosDeviceW`）**：同时支持 **NTFS** 与 **Windows 11 ReFS Dev Drive** / VHDX 固定开发卷（`DRIVE_FIXED` + `FILE_PERSISTENT_ACLS`），自动解析祖先目录联接，并忽略 OneDrive 云同步重解析标签（`IO_REPARSE_TAG_CLOUD*`），使同步目录与 Dev Drive 开箱即用。当工作区祖先目录（如 `C:\Users` 或 `%USERPROFILE%`）缺少 `S-1-15-2-1` 时，Virgo 在当前登录会话内映射一个非特权虚拟盘符（`Z:`..`M:`）指向规范工作区路径，避免 `pwsh`、`powershell.exe`、`cmd dir` 与 `pytest` 因遍历 `C:\Users` 触发 `Access Denied`。
5. **预写状态日志与崩溃恢复**：在修改任何 DACL 前，Virgo 先以原子方式将租约状态（包含 `owner_pid`、`owner_create_time` `FILETIME`、映射盘符 `dos_drive`、已授权根目录与保护前 DACL 快照）写入 `%LOCALAPPDATA%\Virgo\sandbox-sessions\`（可通过 `VIRGO_WINDOWS_SANDBOX_STATE_DIR` 覆盖）。即使进程被 `TerminateProcess` 强杀，下次启动时 `recover_stale_sessions()` 也会自动幂等地撤销残留 ACE、解除虚拟盘符、删除孤立 AppContainer 配置并清理日志文件。

## 运行条件

- 本地支持持久 ACL 的固定卷（**NTFS** 或 **ReFS Dev Drive**）上的原生 x64 **Windows 11**，且 `null.sys` 已更新并启用 WIL Feature `60692039`（`10.0.26100.8972+`，使 `\Device\Null` 原生授予 `ALL APPLICATION PACKAGES` `S-1-15-2-1` 读写权限；无需管理员权限）。在不受支持的 Windows 版本（Windows 10、旧版 Windows 11、默认 Windows Server 2022/2025）上，Virgo 默认以 **No-Limit** 启动并提示选择 **1. WSL2**、**2. No-Limit** 或 **3. 更新 Windows**；显式请求 `workspace-edit` 或 `read-only` 时会 fail-closed 返回 `windows_null_device_unavailable`。
- Windows 筛选平台与防火墙服务（`BFE`、`MpsSvc`）处于运行状态（若未运行，Virgo 会显式失败关闭并返回 `windows_network_isolation_unavailable`）
- 原生 x64 Python 3.11+（用户自有安装如 `uv` 管理的 Python 或 `%LOCALAPPDATA%\Programs\Python`，或继承 AAP 的 `C:\Program Files` 安装——不能使用 WindowsApps 商店存根）以及 PowerShell 7（`pwsh`）或 Windows PowerShell

## 如实说明的平台差异与已知限制

1. **私有目录与 `$HOME` 工作区**：用户目录（`~/.virgo`、`~/.ssh`、`~/.aws`、`Desktop`、`Documents`）默认不包含 `S-1-15-2-1`，无需修改 `~/.virgo` 即可由默认 NTFS ACL 拒绝读写。在原生 Windows 上不允许以 `$HOME`（或包含私有凭据目录的路径）作为工作区，以免授权整个用户目录。
2. **网络隔离与本机回环（`127.0.0.1` / `[::1]`）**：**Read-Only**（`capabilities=()`）通过 WFP 阻止全部出站 TCP、UDP、DNS 与本机回环；**Workspace-Edit**（`internetClient` + `privateNetworkClientServer`）放行公网与内网出站（`git`、`pip`、`uv`、HTTPS）并保留 `HTTP_PROXY` / `HTTPS_PROXY`，**但在非管理员 AppContainer 中本机回环（`127.0.0.1` 与 `[::1]`）仍会被 Windows 阻止**（`CheckNetIsolation` 豁免需管理员权限）。若任务需要访问本地开发服务器、数据库或本机回环代理，请使用一次性 **No-Limit** 或 [WSL2](wsl2.md)。
3. **缺少 AAP 且属主为管理员/SYSTEM 的运行时目录（`windows_runtime_inaccessible`）**：`C:\Windows\System32` 与标准 `C:\Program Files` 默认继承 `S-1-15-2-1 (RX)`，用户自有运行时与 `PATH` 工具链则由会话授予只读 ACE。对于 Virgo 自身 Python 运行时目录（`sys.prefix` / `sys.base_prefix`）及直接启动的可执行文件（`argv[0]`，如 `pwsh.exe`），若目录缺少 AAP 且非提权用户无 `WRITE_DAC` 权限，Virgo 会在启动前显式失败关闭（`windows_runtime_inaccessible`），并引导改用用户自有安装（`uv python install`、`%LOCALAPPDATA%\Programs\Python`）或继承 AAP 的运行时。
4. **尚不存在的 `.virgo`**：文件系统对象 ACL 仅能作用于已存在的对象，因此可写工作区内的 Shell 命令仍可新建原本不存在的 `.virgo`（与 Linux `bubblewrap` 只读 bind 挂载的固有限制相同）。Broker 文件工具（`PermissionPolicy`）与不可信项目配置剥离仍对其进行保护。
5. **上游工具与 ACL 继承注意事项**：
   - **CPython 3.13 的 `os.mkdir(mode=0o700)` / `tempfile.mkdtemp()`**：Windows 上 CPython 3.13 在传入 `0o700` 时会创建显式 `SE_DACL_PROTECTED` DACL（`D:P(...OW)`）并省略 AppContainer SID，导致 AppContainer 子进程无法访问自己刚建好的 `0o700` 目录（例如 `pytest` 默认 `cacheprovider` 会触发 `PytestCacheWarning`，传 `-p no:cacheprovider` 可规避）。普通 `os.mkdir()`（默认 `0o777`）可正常继承父目录 DACL。
   - **`C:\Users` 下的 `git init` 与仓库外 `git` 命令**：Git 的 `strbuf_realpath()` 会将虚拟盘符还原为 `C:\Users\...`，进而因祖先目录 `stat()` 返回 `EACCES`。在已有 Git 仓库内，`git status`、`git diff`、`git log`、`git add`、`git commit`、`git checkout` 与 `git ls-remote` 均可正常运行。
   - **断开继承（`SE_DACL_PROTECTED`）的工作区子目录**：在会话建立前已断开继承的子目录会被根目录授权跳过（沙箱内不可访问）；在会话建立后才断开继承的子目录在仅撤销根目录后会残留显式 SID，需显式撤销清理（但删除 AppContainer 配置后该 SID 不会对后续会话生效）。

另见：[执行边界与沙箱](sandbox.md) · [WSL2 使用指南](wsl2.md) · [原生执行验收](../development/execution-acceptance.md) · [第一阶段原型验证报告（英文）](../../en/development/windows-appcontainer-prototype.md)
