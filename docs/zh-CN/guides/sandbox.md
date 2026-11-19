# 原生本地执行

执行器是 Core 基础设施。No-Limit 经过同一个执行器使用当前账户的 Host 权限；Read-Only 和 Workspace-Edit 使用原生沙箱后端。两者均允许网页搜索与读取。Read-Only 默认关闭 Shell 网络，Workspace-Edit 允许 Shell 联网。详见[模式与审批](../development/permission-profiles.md)。

可信 Python 调用者独立使用 `BASH`、`run_command()` 或 `spawn()` 而未提供上下文时，会通过 `SandboxExecutor` 明确选择无限制执行。会话工具始终传入来源任务的上下文。`BASH` 和 `run_command()` 将输出保存为 Broker 私有记录；`spawn()` 调用者负责返回的进程句柄及其流。

## 实时工作区

命令、文件工具和宿主编辑器访问同一目录。Read、Write、Edit、Glob、Grep 使用事先保存的可信 Helper 副本，通过与 Shell 相同的执行器运行。Helper 解析目标路径、保留打开对象的身份、暂存新内容并原子替换目标。编辑器和运行中的监听器可以立即重新打开该路径读取结果。每条命令拥有私有临时目录和 HOME。

所有模式都将命令输出保存到 Virgo 用户目录中的 Broker 私有记录。Shell 只获得管道，结果返回不透明的 `output_id`。使用 `read_output` 分页读取来源会话拥有的记录；文件工具不因此获得私有输出目录的访问权。详见[文件 Helper 与输出记录](../development/filesystem-helper.md)。

Linux 需要系统提供 Bubblewrap、libseccomp 和创建命名空间的权限。macOS 使用系统 `sandbox-exec` 和 launchd 监督命令及其后代。Windows 使用 LPAC、受限令牌、临时 ACL 和 Job Object。如果平台无法建立请求的边界，本次操作返回错误。

任务的允许规则不是各平台完全一致的读取白名单。Linux 和 macOS 还开放启动载荷所需的系统、解释器及可信 Worker 依赖；Windows 保留环境 LPAC 能力原本可读取的资源。后端支持的显式拒绝规则优先于这些读取权限；无法实施的拒绝规则会在载荷启动前被拒绝。

Read-Only 下的 Shell 命令如需联网，可为本次调用申请联网权限。网络策略使用布尔值，方向列表会被拒绝。运行中的进程保持启动时获得的权限。

Linux seccomp 在两种联网策略下都拒绝 Unix `socket()`，仅允许 `socketpair(AF_UNIX, SOCK_STREAM, 0)` 及可选的 `SOCK_CLOEXEC`、`SOCK_NONBLOCK` 标志，使 asyncio 能唤醒自己的事件循环。未知标志、其他地址族、非零协议以及 datagram/seqpacket pair 均被拒绝。这些 stream 端点创建时已连接；[Linux Unix socket 实现](https://github.com/torvalds/linux/blob/v6.8/net/unix/af_unix.c) 不允许它们通过 `AF_UNSPEC` 断开、重连宿主 socket 或变成监听器。Datagram pair 的重连语义不同，仍被阻止。`socketcall` 与 `io_uring` 也继续被拒绝。

Windows 上，[CPython 的 `_overlapped` 模块](https://github.com/python/cpython/blob/v3.13.17/Modules/overlapped.c) 在导入 asyncio 时创建 TCP socket，以初始化 Winsock 函数指针。之后的 [socketpair 实现](https://github.com/python/cpython/blob/3.13/Lib/socket.py) 还会通过回环 TCP 建立事件循环的唤醒通道。因此 stdio 服务器仍需能在来源 LPAC 与网络策略下初始化的运行时。任何原生后端都不会自动扩大策略或使用 Host 权限重试服务器。详见 [Windows 实测范围](windows-sandbox.md)。

## 文件保护

显式拒绝读取同时拒绝写入；仅拒绝写入仍允许读取。拒绝规则优先于任何重叠的允许规则。原生策略编译要求显式拒绝根是确实存在的文件或目录，无法实施的根会在载荷启动前被拒绝。已有的控制元数据与可信执行运行时获得写入拒绝规则；这并不承诺保护任意尚不存在的保留名称。

原生后端能够建立必要的隔离时，工作区可以包含解释器或 Virgo 私有存储，也可以是用户 HOME。运行时代码保持只读，私有存储不可访问。受保护根的祖先目录不能被重命名或删除，以免受保护子树被移动到可写路径；普通同级文件仍可使用。包含操作系统挂载根的工作区仍不受支持。

POSIX 不再在启动时扫描整个可写目录树的硬链接。原生路径与挂载限制不能保证预存的可写硬链接不会修改授权目录之外同名 inode 的内容。Filesystem Helper 的修改使用保留打开对象和原子替换；Shell 命令对预存别名的操作没有同样的保证。Windows 保留递归 ACL 与硬链接校验。实时工作区要求宿主写入者在命令运行期间维持文件系统边界；恶意宿主写入者可以在沙箱之外改变路径身份。

额外的 Shell 路径必须是确实存在的文件或目录，单个文件的授权不会扩展到父目录。原子 Write/Edit 要求明确批准目标目录的写权限。Windows 上替换后的文件继承父目录 ACL，不保留旧文件自定义的显式文件级 ACL。

Broker 和可信扩展使用各自的操作系统权限。内置文件工具在 No-Limit 下也通过 Helper 执行。发布验收以最终提交的原生测试和完整跨平台比较为准。见[验收记录](../development/execution-acceptance.md)和 [Windows 平台范围](windows-sandbox.md)。
