# 原生 Windows 命令沙箱

> **状态：一级支持（`windows-appcontainer`）。** 原生 Windows 默认启用 **Workspace-Edit**（`execution.mode = "workspace-edit"`），**无需管理员权限**即可非提权运行，适用冷启动（`<= 5 秒`）与 O(1) 单条命令性能门槛，并纳入 PR 阻塞验收 CI。如果你偏好 Linux 工具链或需要在沙箱命令中访问本机回环服务（`127.0.0.1`），[WSL2](wsl2.md) 同样是一级支持方案。

> Virgo 的沙箱是防止模型偶发误操作的**护栏**，不是抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行。详见[执行边界与沙箱](sandbox.md)。

## 标准 AppContainer 后端工作原理

Virgo 已移除旧版仅限管理员的 LPAC 后端，全面切换为**标准 AppContainer**（`src/virgo/sandbox/windows_sandbox.py`、`windows_acl.py`、`windows_files.py`）：

1. **非提权标准 AppContainer**：直接通过 `CreateProcessW`（`CREATE_SUSPENDED` + `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` + `HANDLE_LIST` + `JOB_LIST`）启动子进程，保留 `ALL APPLICATION PACKAGES`（`S-1-15-2-1`）。不再启用 LPAC 退出标志（`0x2000F`），不再使用 `CreateRestrictedToken` / `WRITE_RESTRICTED`，不再修改 `\Device\KsecDD` 或 `\Device\Null` 设备 DACL，不再创建 `O:BAG:BA` 管理员互斥锁，不再修改 WindowStation/Desktop DACL，也不写入 Windows 防火墙规则。恢复线程（`ResumeThread`）前会校验挂起令牌（`TokenIsAppContainer == 1`、包 SID 与能力 SID 匹配、`TokenRestrictedSids` 为空、`S-1-15-2-1` 放行且仅 Everyone 拒绝）。
2. **会话级单 SID + OS 自动继承 ACE**：同一 `(workspace, mode)` 会话复用单个 AppContainer 配置与 SID，通过单次 `SetNamedSecurityInfoW` 由操作系统传播可继承 ACE（无 Python 逐文件 ACL 遍历或硬链接扫描），将冷启动与首次命令初始化控制在 5 秒内、重复命令保持 O(1) 启动开销。每条命令拥有独立的 `TMPDIR`（位于 `<AC>\Temp` 下），在 Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）排空整棵进程树后立即清理；Windows AppContainer 自动重写的 `TEMP`/`TMP`（`<AC>\Temp`）按会话生命周期共享并在会话关闭时删除。
3. **控制元数据写保护（VER-01）**：对已存在的 `.git`、`.virgo`、`AGENTS.md`、`CLAUDE.md` 及只读子路径采用 `protected_readonly`（设置 `SE_DACL_PROTECTED`，保留全部非会话宿主 ACE 以确保宿主 `git` 与编辑器原子保存不出现 `WinError 5`，并将本会话 SID 限制为显式 `ALLOW (RX) 0x001200A9`——不使用 `ACCESS_DENIED_ACE`，因为 Windows 内核在 AppContainer 检查中会忽略针对 `S-1-15-2-*` 的拒绝项）。可写工作区根目录省略 `FILE_DELETE_CHILD`（`0x40`），仅以纯继承方式授予 `DELETE`（`0x00010000`），防止通过父目录权限删除或重命名受保护子项。
4. **祖先目录虚拟盘符（`DefineDosDeviceW`）**：当工作区祖先目录（如 `C:\Users` 或 `%USERPROFILE%`）缺少 `S-1-15-2-1` 时，Virgo 在当前登录会话内映射一个非特权虚拟盘符（`Z:`..`M:`）指向已校验的规范工作区路径，并以该盘符作为子命令工作目录，从而避免 `pwsh`、`powershell.exe`、`cmd dir` 与 `pytest` 收集阶段因遍历 `C:\Users` 触发 `Access Denied`。
5. **预写状态日志与崩溃恢复**：在修改任何 DACL 前，Virgo 先以原子方式将租约状态（包含 `owner_pid`、`owner_create_time` `FILETIME`、映射盘符 `dos_drive`、已授权根目录与保护前 DACL 快照）写入 `%LOCALAPPDATA%\Virgo\sandbox-sessions\`（可通过 `VIRGO_WINDOWS_SANDBOX_STATE_DIR` 覆盖）。即使进程被 `TerminateProcess` 强杀，下次启动时 `recover_stale_sessions()` 也会自动幂等地撤销残留 ACE、解除虚拟盘符、删除孤立 AppContainer 配置并清理日志文件。

## 运行条件

- 本地 **NTFS** 卷（支持持久 ACL）上的 x64 Windows 10/11 或 Windows Server（无需管理员权限）
- Windows 筛选平台与防火墙服务（`BFE`、`MpsSvc`）处于运行状态（若未运行，Virgo 会显式失败关闭并返回 `windows_network_isolation_unavailable`）
- 原生 x64 Python 3.11+（用户自有安装如 `uv` 管理的 Python 或 `%LOCALAPPDATA%\Programs\Python`，或继承 AAP 的 `C:\Program Files` 安装——不能使用 WindowsApps 商店存根）以及 PowerShell 7（`pwsh`）或 Windows PowerShell

## 如实说明的平台差异与已知限制

1. **私有目录与 `$HOME` 工作区**：用户目录（`~/.virgo`、`~/.ssh`、`Desktop`、`Documents`）默认不包含 `S-1-15-2-1`，无需修改 `~/.virgo` 即可由默认 NTFS ACL 拒绝读写。在原生 Windows 上不允许以 `$HOME`（或包含 `~/.virgo` 的目录）作为工作区，以免授权整个用户目录。
2. **网络隔离与本机回环（`127.0.0.1` / `[::1]`）**：**Read-Only**（`capabilities=()`）通过 WFP 阻止全部出站 TCP、UDP、DNS 与本机回环；**Workspace-Edit**（`internetClient` + `privateNetworkClientServer`）放行公网与内网出站（`git`、`pip`、`uv`、HTTPS），**但在非管理员 AppContainer 中本机回环（`127.0.0.1` 与 `[::1]`）仍会被 Windows 阻止**（`CheckNetIsolation` 豁免需管理员权限）。若任务需要访问本地开发服务器、数据库或本地 HTTP 代理，请使用一次性 **No-Limit** 或 [WSL2](wsl2.md)。
3. **缺少 AAP 且属主为管理员/SYSTEM 的运行时目录（`windows_runtime_inaccessible`）**：`C:\Windows\System32` 与标准 `C:\Program Files` 默认继承 `S-1-15-2-1 (RX)`，用户自有运行时则由会话授予一次只读 ACE。对于 Virgo 自身 Python 运行时目录（`sys.prefix` / `sys.base_prefix`）及直接启动的可执行文件（`argv[0]`，如 `pwsh.exe`），若目录缺少 AAP 且非提权用户无 `WRITE_DAC` 权限（例如安装在副盘且属主为 `Administrators` 的 Conda、指向副盘的目录联接，或断开继承的 `Program Files` MSI 安装目录），Virgo 会在启动前显式失败关闭（`windows_runtime_inaccessible`），并引导改用用户自有安装（`uv python install`、`%LOCALAPPDATA%\Programs\Python`）或继承 AAP 的运行时。在 Shell 脚本内部间接调用的其他非 AAP 管理员目录工具不会在启动前逐一预检，将在子进程内按 Windows 默认权限拒绝。
4. **尚不存在的受保护文件名**：NTFS ACL 仅能作用于已存在的文件系统对象，因此可写工作区内的 Shell 命令仍可新建原本不存在的 `CLAUDE.md` 或 `.virgo`（与 Linux `bubblewrap` 只读 bind 挂载的固有限制相同）。Broker 文件工具（`PermissionPolicy`）与不可信项目配置剥离仍对其进行保护。
5. **上游工具与 NTFS 继承注意事项**：
   - **CPython 3.13 的 `os.mkdir(mode=0o700)` / `tempfile.mkdtemp()`**：Windows 上 CPython 3.13 在传入 `0o700` 时会创建显式 `SE_DACL_PROTECTED` DACL（`D:P(...OW)`）并省略 AppContainer SID，导致 AppContainer 子进程无法访问自己刚建好的 `0o700` 目录（例如 `pytest` 默认 `cacheprovider` 会触发 `PytestCacheWarning`，传 `-p no:cacheprovider` 可规避）。普通 `os.mkdir()`（默认 `0o777`）可正常继承父目录 DACL。
   - **`C:\Users` 下的 `git init` 与仓库外 `git` 命令**：Git 的 `strbuf_realpath()` 会将虚拟盘符还原为 `C:\Users\...`，进而因祖先目录 `stat()` 返回 `EACCES`。在已有 Git 仓库内，`git status`、`git diff`、`git log`、`git add`、`git commit` 与 `git ls-remote` 均可正常运行。
   - **断开继承（`SE_DACL_PROTECTED`）的工作区子目录**：在会话建立前已断开继承的子目录会被根目录授权跳过（沙箱内不可访问）；在会话建立后才断开继承的子目录在仅撤销根目录后会残留显式 SID，需显式撤销清理（但删除 AppContainer 配置后该 SID 不会对后续会话生效）。

另见：[执行边界与沙箱](sandbox.md) · [WSL2 使用指南](wsl2.md) · [原生执行验收](../development/execution-acceptance.md) · [第一阶段原型验证报告（英文）](../../en/development/windows-appcontainer-prototype.md)
