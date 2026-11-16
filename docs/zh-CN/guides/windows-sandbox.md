# Windows 命令沙箱

[原生命令沙箱](sandbox.md) · [English](../../en/guides/windows-sandbox.md)

## 验证状态

在提交 `e2e5c94ca9cb4e336092963a11f977cfcc4ee52b` 上，下面两组真实 Windows 环境均通过全部 18 项安全场景，且各自的 XML 报告确认零失败、零错误、零跳过。参见[对应的原生 CI 运行](https://github.com/0xAstraZhang/Virgo/actions/runs/43747132842)。

| 已验证 runner | OS build | PowerShell | Python | 原生验收结果 |
| --- | --- | --- | --- | --- |
| Windows Server 2022 Datacenter，x64 | 20348 | 7.6.10 | CPython 3.13.17，x64 | 18 项通过 |
| Windows Server 2025 Datacenter，x64 | 26100 | 7.6.9 | CPython 3.13.17，x64 | 18 项通过 |

这些结果对应使用本地 NTFS、以管理员身份运行的 GitHub 托管 runner，不能证明桌面 Windows、ARM64／模拟执行或普通账户兼容性。完整跨平台对比是另一个独立门禁；应按[测试与 CI](../development/testing-and-ci.md)检查当前 PR 修订。

下面记录已执行验收的权限模型、宿主前提和剩余限制。

## 权限模型

审批允许 Core 工具请求执行，不能把启用的沙箱变成普通进程。工作模式和子任务限制独立决定权限上限。工作区来自来源 Runtime；切换当前会话或改变命令工作目录不会更换授权根目录。

| 机制 | 已实现的职责 | 边界限制 |
| --- | --- | --- |
| LPAC | 每次命令创建独立 AppContainer 身份，关闭 `ALL_APPLICATION_PACKAGES` 授权，赋予 `registryRead` 和 `lpacInstrumentation` capability | Windows 仍有 `ALL RESTRICTED APPLICATION PACKAGES` 的系统授权。这是对象 ACL 隔离，不是文件系统命名空间，也不是精确的读取路径白名单。 |
| 写受限 Token | 使用 `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE \| WRITE_RESTRICTED)`，仅加入一个独立随机限制 SID | 附加检查约束写入；它本身不约束读取和网络。不把 Everyone 或 logon SID 加为替代写授权。 |
| 临时文件 ACL | 为工作区、选定运行时和私有 profile 添加每次命令独立的读／执行或写 ACE；对只读及受保护对象明确拒绝修改 | 宿主必须能修改相应对象的 ACL。这会临时修改 DACL，不会挂载或复制私有文件系统。 |
| AppContainer 网络策略 | 不提供网络 capability；启动要求 Base Filtering Engine 和 Windows Firewall 服务正在运行 | 未实现域名白名单、代理隔离边界或启用沙箱时的无限制网络模式。 |
| 加密设备临时授权 | 在精确的 `\Device\KsecDD` 对象上，为命令的独立写 SID 临时添加 `0x00100003` 允许 ACE | 授予该设备的文件读写数据与同步权限，不是 BCrypt 函数或单独 IOCTL 的白名单；不会修改 ETW provider 的 ACL。 |
| Null 设备临时授权 | 在精确的 `\Device\Null` 对象上，为命令的 package SID 和写 SID 临时添加 `0x0012019F` 允许 ACE | 允许 `NUL` 重定向及 `subprocess.DEVNULL` 使用的通用读写访问；不授予设备所有权、DACL 修改、删除或其他设备对象的访问权限。 |
| 私有桌面 | 每次命令拥有独立的 Low 完整性级别桌面；在当前 window station 上仅向命令身份授予属性／全局原子访问权限 | 临时修改当前 station 的 DACL；不授予剪贴板、屏幕、宿主桌面访问或修改桌面安全配置的权限。 |
| Job Object | 原子分配进程、跟踪后代生命周期并终止进程树 | Job 不负责文件、网络和进程访问权限隔离。 |

`lpacInstrumentation` 在整个 Token 上生效，可用于 Windows 已向该 SID 授权的对象，包括 ETW provider 注册；其范围不局限于启动诊断中观察到的两个 provider。文件系统写入仍需通过独立限制 SID 的检查。

`read-only` 请求将工作区设为命令只读，同时保留私有可写 home/temp。宿主还持有来源工作区 `.virgo/outputs` 下的精确日志句柄。宿主数据读取受 LPAC ACL 和显式运行时／工作区读授权约束，不能套用 POSIX 后端的挂载列表来描述。

## 环境与路径要求

后端发现要求 Windows 10 版本族上的原生 x64 进程，以及实现使用的原生接口。这只是接口预检查，不代表所有 Windows 10/11 或 Server 版本都兼容；已验证组合见上表。桌面 Windows、ARM64／模拟执行及普通账户安装方式尚未验证。

启用 Windows 沙箱时只接受 PowerShell 7（`pwsh.exe`）。Windows PowerShell 5.1、Git Bash 和 WSL 会被该后端拒绝。选择 `disabled` 时沿用现有 Windows Shell 执行器，不宣称隔离。

宿主需要每个临时授权对象的 `READ_CONTROL` 和 `WRITE_DAC`，包括所选 PowerShell 安装目录、Python 解释器及虚拟环境目录。因此，普通账户可能无法使用安装在 `Program Files` 下的运行时。实现不会提权或安装特权服务；管理员 CI 运行不能证明普通账户兼容性。宿主还必须能够创建私有桌面、修改当前 window station 的 DACL；不会降低宿主 station 的完整性级别。设备授权还要求机器共享 KsecDD 和 Null 设备的 `READ_CONTROL | WRITE_DAC`，以及各自由 Administrators／SYSTEM 拥有、DACL 受保护的协调 mutex。在已测 runner 上，这需要管理员权限。缺少权限或发现不兼容的预建 mutex 会拒绝执行，不接管所有权、不提权，也不替换该对象的策略。

授权与输出路径必须是支持持久 ACL 的本地 NTFS 直接盘符绝对路径。UNC、映射／替代盘符、设备路径、备用数据流及非法／保留文件名会被拒绝。原生文件层将已检查的盘符转换成本地卷设备路径，通过 `NtCreateFile` 的禁止重解析语义打开；修改 ACL 使用保留的对象句柄，不重新按可变路径打开目标。

工作区中的 junction／reparse point 和多硬链接文件会拒绝启动，`read-only` 也如此。运行时根目录必须是直接路径；只读运行时目录内的重解析别名会被跳过，不遍历或授权其目标。已有根级 `.git`、`.virgo`、`AGENTS.md`、`CLAUDE.md` 和解释器路径设置修改拒绝；尚不存在的元数据名称没有被该 Windows 实现预留。

只有工作区、选定 PowerShell／Python 安装目录和私有 profile 获得显式的每次命令文件授权。其他 SDK、解释器和工具链可能不可访问，除非 Windows 已经向 LPAC 身份授权。当前未提供附加运行时根目录配置。

实测沙箱启动中，CPython 3.13.17 会输出 `Failed to find real location of ...`。两台 runner 上，CPython 所用的精确可执行文件打开成功，`GetFinalPathNameByHandleW` 能返回同一句柄的 NT 路径，而使用 flags `0` 和 `8` 的 DOS 路径请求返回访问拒绝；受控 Python 命令仍正常退出。这是已观察到的 DOS 路径规范化限制，不代表可执行文件不可读或不能运行；依赖该 API 的其他软件可能失败。Shell 保留原始诊断和退出状态，后端不会为消除提示而扩大祖先目录或设备命名空间权限。参见[提交 `4fe5a51` 的原生观测](https://github.com/0xAstraZhang/Virgo/actions/runs/43745493847)、[CPython 启动路径处理](https://github.com/python/cpython/blob/v3.13.17/Modules/getpath.py)和 [Windows API 契约](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew)。

## 启动与清理

前台 Shell、后台命令和子 Agent 沿用 `SandboxPolicy.spawn` 与 `WindowsProcess`。后端挂起创建实际命令，并在创建时分配 Job；恢复一次之前检查实际进程 Token 的 package SID、限制 SID、精确 capability 集合及 LPAC 访问语义。后者使用真实 `AccessCheck` 检查 AAP／ARAP 描述符，不使用在两组 runner 上失败的 class-46 Token 查询。

恢复进程前的原生 API、ACL 或 Token 验证失败会拒绝启动。Windows 没有业务命令启动确认信号；恢复后发生的 DLL 或托管运行时初始化失败，按普通进程退出报告非零状态及已有输出，不会导致在沙箱外重试。

只继承明确列出的 stdin/stdout 句柄。环境变量白名单忽略大小写，保留可执行文件搜索和区域设置，补充固定 Windows 运行时变量，并将 home/temp 指向私有 profile；不会转发厂商密钥、代理凭据或任意继承变量。明确放入已授权工作区的凭据不因此隐藏。

工作区、cwd 和输出目录的保留句柄禁止删除对应目录，但允许写共享。[Windows 在重命名时以 `FILE_ADD_FILE` 打开目标目录](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/87f86c9b-6c2a-4803-84b7-131a74a434fa)；禁止写共享也会阻止对普通子文件的合法替换。这些共享标志不替代禁止 reparse 的路径检查或受保护元数据的只读 ACL。精确输出文件句柄仍禁止删除共享。

根进程正常退出、超时和取消均清空同一个 Job，包括比根进程存活更久的后代。只有根进程对象已发出退出信号、内核 Job 计数为零时，才释放 ACL／profile 资源。并发命令使用不同身份；清理从当前 DACL 中仅移除本次命令的 ACE，不回写旧的完整 ACL 快照。文件／station 更新由每用户 mutex 协调，KsecDD 和 Null 更新各自由另外经过校验的机器范围 mutex 协调。这些锁协调 Virgo 实例；ACL 读取、合并、写入不是对外部管理员策略变更的事务。每个设备及其协调句柄会一直保留到 Job 清空且本命令的设备 ACE 已移除。启动或清理失败会报告错误，不能回退到普通进程或自动重放命令。

宿主突然退出会关闭 Job 句柄，由 `KILL_ON_JOB_CLOSE` 请求内核终止进程；但崩溃后不会运行 Python ACL／profile 清理。临时文件、window station 或设备 ACE 及 AppContainer profile 可能保留。当前没有恢复日志或持久清理服务，每次命令身份独立且不复用。未提供资源配额，也不防御有特权的管理员修改宿主安全配置。

Core `write`／`edit`、可信扩展、hooks、远程工具和宿主本身在 LPAC 外执行。Core 文件修改保留独立的应用层检查和新文件暂存；Windows 路径不具备 POSIX 描述符相对遍历保证。本后端不宣称这些 Python 操作受 OS 沙箱隔离。

Core 文件提交的最后一步使用原生 `FileRenameInformationEx` 的替换和 POSIX 语义标志，原子替换目录项，同时允许运行中的命令继续保留旧文件的 ACL 句柄用于清理。暂存源文件和目标父目录均以禁止 reparse 遍历的方式打开，并保留到重命名完成。原生重命名不可用时提交失败，不会关闭其他命令的 ACL 句柄或改写现有目标 inode。参见[原生重命名契约](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)。

## 分发与维护

未新增第三方依赖、驱动、编译助手、服务、账户预配或防火墙规则安装器。普通 Python 包分发 `windows_sandbox.py`、`windows_sandbox_acl.py`、`windows_sandbox_files.py`，由 `ctypes` 调用系统 DLL；进程生命周期仍由 `windows_process.py` 管理。

无需单独分发原生二进制，但项目需要维护 Windows ABI 声明、对象／句柄生命周期、每次命令的 ACL 遍历和并发清理逻辑。每次启动都会遍历授权树。边界变更必须运行真实 Windows 验收，导入检查、lint 或模拟拒绝不能代替它。

## 验收与参考

`tests/harness/test_windows_sandbox_e2e.py` 的 18 个场景先于生产后端提交，覆盖工作区正向访问、工作区外与系统包 ACL、只读及元数据上限、重解析与硬链接、凭据与句柄继承、后代 Token 及进程访问、真实 TCP／UDP、超时／取消、后台与子 Runtime 来源、禁止重放及资源清理。专用 workflow 在 `windows-2022` 和 `windows-2025` 上运行精确 PR head，任何失败、错误或跳过都会拒绝验收。LPAC 验证器修正只将不受支持的 Token 查询换成原生 `AccessCheck`，保留原有 18 个场景与断言。针对实测 BCrypt 及空设备流访问失败，又在这些场景中加入设备授权验收定义，并先于各自设备修复提交：父子进程设备权限、并发命令间无关 ACE 保留、正常／超时／取消清理、坏映像回滚，以及不可信协调 mutex 的拒绝。

- [Microsoft：启动 AppContainer／LPAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- [Restricted Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens) 与 [CreateRestrictedToken](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken)
- [AccessCheck](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-accesscheck) 与 [CreateProcessAsUserW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
- [原生文件／设备权限](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/nf-wdm-zwcreatefile)、[CreateMutexExW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexexw) 与 [ETW provider 权限](https://learn.microsoft.com/en-us/windows/win32/api/evntcons/nf-evntcons-eventaccesscontrol)
- [Window station 访问权限](https://learn.microsoft.com/en-us/windows/win32/winstation/window-station-security-and-access-rights)、[桌面访问权限](https://learn.microsoft.com/en-us/windows/win32/winstation/desktop-security-and-access-rights) 与 [CreateDesktopW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-createdesktopw)
- [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [已读取的 Codex Windows 实现修订](https://github.com/openai/codex/tree/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/windows-sandbox-rs)：其旧受限 Token 路径和提权账户／防火墙路径采用不同权限模型。Virgo 不将代理环境变量或 Job Object 当作网络隔离。
