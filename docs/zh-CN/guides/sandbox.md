# 原生命令沙箱

[配置参考](../reference/configuration.md) · [English](../../en/guides/sandbox.md)

## 启用与查看

默认权限预设为 `Workspace-Edit`：允许编辑、仅工作区、允许联网。`Read-Only` 保留工作区范围与联网能力，禁止修改宿主数据；`No-Limit` 移除 Virgo 的原生隔离，但仍受宿主账户和操作系统权限限制。所需后端缺失或无法建立边界时拒绝执行，不会静默切换为宿主执行。

```bash
uv run virgo
uv run virgo --mode read-only
uv run virgo --mode no-limit
```

用户配置（`$VIRGO_HOME/settings.json`，默认 `~/.virgo/settings.json`）可以选择或自定义预设：

```json
{"permissions": {"mode": "workspace-edit", "modes": {"offline": {"name": "Offline", "readOnly": false, "workspaceOnly": true, "networkAllowed": false}}}}
```

使用 `/mode` 查看、切换预设；权限编辑器可以修改名称和三个开关。项目配置不能扩大用户权限。显式旧配置 `--sandbox read-only`、`--sandbox workspace-write` 或 `sandbox.mode` 保留原有禁网行为；`disabled` 对应 No-Limit。旧 `sandbox.network` 被拒绝，并提示改用 `permissions.modes.<id>.networkAllowed`。未知模式和字段会导致配置失败。

`/sandbox` 显示当前 Runtime 的实际权限与后端发现状态；如果运行中任务与新选中的预设不同，还会显示下次运行的预设。找到启动器只表示发现后端，不能证明内核允许隔离。每次实际启动都要验证边界；Windows 还验证挂起进程的 Token 和 Job 归属。显式拒绝规则与只读子任务上限仍然适用。

Workspace-Edit 允许文件访问范围内的联网操作。外部 SDK／缓存和受保护 Git 元数据仍可能阻止命令。模型可以调用 `request_permissions` 为一个参数确定的操作申请额外权限；批准不会修改整个对话的预设，也不会执行或重放命令。修改受保护控制路径需要明确申请单次 No-Limit 操作。Windows 在已验证 runner 配置之外仍是实验性支持。

## 执行边界

`read_only` 控制宿主数据写入，`workspace_only` 控制用户文件访问范围；解释器目录与既有控制元数据保留固定保护。工作区受限时，Linux/macOS 暴露下文所列系统／运行时目录；Windows 使用对象 ACL，并保留系统 LPAC 读取授权，详见 [Windows 命令沙箱](windows-sandbox.md)。每条命令都有可写临时目录和私有 HOME。输出日志保存在来源工作区的 `.virgo/outputs`，即使从其他获准目录运行也不改变归属。宿主在记录前检查路径并保留精确输出描述符。子进程仅继承 PATH、语言／时区／终端变量、私有 home/temp 与 VIRGO=1（Windows 另有固定原生运行时变量），不转发继承的厂商密钥。

`network_allowed=false` 时，Linux 使用独立网络命名空间并通过 seccomp 禁止 socket；macOS 不授予 Seatbelt 网络权限；Windows 不提供网络 capability。允许联网时，Linux 共享宿主网络并仅允许 IPv4／IPv6 socket 类型，macOS 授予 TCP／UDP 权限，以及 DNS 所需的精确系统解析器／路由 IPC 端点。任意 Unix socket 和进程控制逃逸路径仍受限制。Linux 只额外暴露精确解析器配置文件，不暴露 `/run` 下的宿主服务 socket。Windows 增加互联网／私有网络 capability，不修改防火墙规则或 loopback 豁免。联网权限可以访问宿主 TCP 服务，不是域名或远端主机白名单。Core 在应用层另行约束 Web／MCP 操作；模型传输仍在命令沙箱之外。

前台、后台及子 Runtime Shell 共用启动器，使用来源工作区。Plan 和 Code Review 的提示词不会更改三个开关，也不删除常规工具。明确指定的只读子任务可以收紧继承权限。已运行的进程保留启动时边界；切换预设不会追溯改变其权限。

启用沙箱还会阻止 Shell 发现与选择过程中的宿主执行。启动配置、PATH 候选、缓存选项和 `/shell` 命令只检查可执行路径，不运行版本探测；版本保持为 `unknown`，由实际隔离启动判断所选解释器能否执行。后端不可用时也不会回退到宿主版本探测。相应的版本与兼容行为见[命令解释器](shells.md)。

allow 规则和普通工具审批不会关闭沙箱；只有明确绑定到单个操作的权限申请可以改变该操作的开关。缺少启动器／系统库、不支持的平台、命名空间拒绝或 Seatbelt 配置失败返回 `sandbox_unavailable`，不在宿主直接执行业务命令。探测成功不等于实际启动成功：Linux/macOS 在实际沙箱内运行启用隔离启动选项的固定 Python 启动程序，通过管道确认命令已完成 exec。进程启动后若无法可靠确认状态，包括确认信号超时，结果为 `sandbox_failed` 并明确可能存在副作用；缺少确认信号不能证明命令没有运行。Windows 挂起创建实际进程，检查其 Token 和 Job 归属后仅恢复一次；恢复前的原生配置失败单独报告。Windows 没有业务命令启动确认信号：恢复后发生的 DLL 或运行时初始化失败，与其他非零退出一样按进程退出结果报告。两条路径都不根据 stderr 文本猜测。普通非零退出和超时保留 `command_failed` 事实，并记录 `command_started=true` 与可能存在副作用。超时与取消保留各自状态，也不会自动重放可能已产生部分副作用的操作。Shell 结果记录实际模式与工作区，后台任务结果另外保留后端信息。

## 平台要求与限制

Linux 需要 root 所有的系统 `bwrap`（支持 `--disable-userns`、`--ro-bind-fd`、`--share-net` 与 `--remount-ro`）、系统 `libseccomp.so.2` 和创建相应命名空间的权限。受保护路径通过固定的源描述符和 bubblewrap 的 inode 校验进行只读挂载，不使用未经处理的符号链接路径作为保护挂载的来源。旧后端缺少所需选项时拒绝执行。当前 seccomp 实现支持 x86-64 与 AArch64。

macOS 需要系统 `/usr/bin/sandbox-exec`、`/bin/launchctl` 和 `/usr/bin/env`、允许注册 Background 任务的用户 launchd 域，以及 Darwin 的进程身份、带版本的信号和 coalition 统计接口。这些私有接口会在运行时检查，缺少支持则拒绝执行。每条命令使用唯一的临时任务，不安装持久 LaunchAgent，也不启用自动重启。Windows 后端与当前验证限制见 [Windows 命令沙箱](windows-sandbox.md)。无法建立边界时拒绝执行；`disabled` 保持现有 Windows 执行器。

工作区范围开启时，Linux/macOS 可读范围包括实际存在的系统目录（`/usr`、`/bin`、`/sbin`、`/lib`、`/lib64`、`/etc`、`/opt`；macOS 另含 `/System/Library`、`/System/Cryptexes`、`/System/Volumes/Preboot/Cryptexes/OS`、`/Library`、`/dev` 与 `/private/etc`）、解释器、工作区及私有临时目录，外部 SDK／缓存仍可能不可用。关闭工作区限制会扩大用户文件范围，但保留只读、网络及控制路径规则。POSIX 明确隐藏 Virgo 宿主私有存储；macOS 监督进程状态统一放在被隐藏的 `/private/tmp/virgo-control-<uid>` 下。Windows 保留文档声明的对象 ACL 读取模型，不宣称具有相同的路径遮蔽效果。工作区包含 Virgo 用户凭据／会话目录或系统／基础解释器安装目录时拒绝执行。

支持位于工作区根部以下的活动虚拟环境，例如 `.venv`。可以使用其中已安装的命令和依赖，但 Shell 进程和 Core `write`／`edit` 工具都不能修改其解释器目录。工作区内的解释器目录别名不能是符号链接；`.venv/bin/python` 等正常的 POSIX 可执行文件链接仍可使用。Windows 跳过只读运行时目录下尚未授权的重解析别名，工作区中的重解析路径则拒绝启动。工作区根部本身不能是解释器安装目录。

工作区根部已经存在的 `.git`、`.virgo`、`AGENTS.md` 和 `CLAUDE.md` 不允许由 Shell 写入。上述任一根部元数据路径本身为符号链接时，Linux/macOS 都会拒绝启动 Shell；这些链接不能增加宿主读取挂载，也不能替换受保护的目录项。输出目录中的符号链接同样会在记录输出前导致启动被拒绝。Linux 使用只读挂载保护已存在路径，不能阻止创建尚不存在的这些名称；macOS 也排除尚不存在的名称。Windows 拒绝工作区重解析路径，通过 ACL 保护已有元数据对象，但不预留尚不存在的元数据名称。

在 Linux/macOS 上，启动可写且仅工作区的 Shell 前，Virgo 会遍历工作区的可写部分，发现有多个硬链接的普通文件就拒绝启动。即使其他链接也在工作区内，仍会拒绝，因为链接数无法说明其他名称所在的位置。只读元数据和当前解释器目录不参与检查；`read-only` 模式可以使用共享文件。使用 `cp -al` 或部分包存储布局等方式创建硬链接可写文件的项目，必须先将这些文件改成独立副本，才能使用 `workspace-write`。遍历不会跟随符号链接，也不是针对宿主可信进程并发修改的原子快照。原生命令无法把只读源文件硬链接到可写目录。Windows 在两种活动模式下都会拒绝整个工作区中的多硬链接文件，元数据也参与检查；运行时根目录不参与此项拒绝。

Core `read`／`glob`／`grep` 使用受检查的读取与遍历：POSIX 保留不跟随链接的目录／文件描述符，Windows 保留禁止重解析的原生句柄，并排除宿主私有路径。这些是应用层检查，不将宿主本身放入 OS 沙箱。

Core `write`／`edit` 工具在应用层独立执行工作区、只读、元数据及解释器目录限制，Windows 也适用。启用策略后，已有多个硬链接的文件会被拒绝。修改先写入受保护 `.virgo` 中的新文件，只有请求内容通过校验并写入后才替换目标，因此打开后删除工作区别名也不能让工具修改旧 inode。被拒绝或没有变化的编辑不替换目标；临时文件会被清理。POSIX 使用目录及源文件描述符防止打开期间的符号链接替换，并在提交前检查目标仍指向已打开的源文件。替换保留文件权限位和文本编码、换行格式，但会创建新 inode，不复制所有者或扩展属性；POSIX ACL 也不复制。Windows 保留目标当前的 DACL，新文件则从目标父目录派生 DACL；若替换后的权限收尾失败，错误会明确报告内容已经提交。详见 [Windows 提交与清理](windows-sandbox.md#启动与清理)。临时文件和目标必须位于同一文件系统；跨文件系统替换会失败，且不会修改目标。这些 Python 操作不在 Shell 的 OS 沙箱内。Windows Core 文件工具保留应用层路径检查，不具备 POSIX 的目录描述符遍历；原生 Windows Shell 后端不会将这些宿主操作放入 LPAC。

来源 Runtime 开启工作区范围时，自动加载的项目指令（`AGENTS.md`、`CLAUDE.md`、`.virgo/SYSTEM.md` 与 `.virgo/APPEND_SYSTEM.md`）限制在其来源工作区内，不读取工作区外的祖先目录，也不会因子 Runtime 更换工作目录而导入其他项目的指令。即使当前显示另一个对话或正在重建提示词，也使用该 Runtime 自己的策略。POSIX 读取通过目录描述符进行，不跟随符号链接，创建子任务时同样适用。显式信任的用户指令仍可使用。这项自动上下文边界不提供通用读取授权，也不隔离宿主本身。

### 进程生命周期与清理

Linux 将命令放入独立的 PID 命名空间。macOS 由可信监督进程在独立的 launchd 资源 coalition（内核进程集合）内启动 Seatbelt 命令，宿主在释放命令前检查该身份独立且没有 coalition 管理特权。在宿主释放首条用户命令前，还必须通过一个有寿命上限、无业务副作用的脱离子进程探测；清理证明的截止时间早于探测进程自身的退出期限，自然退出不能算作清理成功。

宿主仍在运行时，普通完成、非零退出、超时、取消和启动失败共用 macOS 清理路径。仅移除精确的 launchd 任务不足以完成清理：宿主还会通过保存的资源 coalition 身份识别剩余成员，并用内核核对 PID 版本的接口发送信号。普通后代创建新会话、进程组或改变父进程，不会改变其 coalition 归属。进程快照本身不能证明完成；必须由内核确认该集合没有活动任务，并确认任务注册已移除，才能删除可写临时目录。不同命令使用独立身份。

清理有截止时间。命令释放后，若无法确认进程控制或清理完成，结果为 `sandbox_failed`；命令可能已经产生副作用，不能重放。未确认的清理会保留临时资源，不通过对象回收自动删除。后台任务即使收到了取消请求，也会明确报告清理失败。主动终止已经确认、但监督进程未能传回命令退出状态时，仍保留准确的超时／取消结果，退出码为未知（`null`），不会编造退出码。

macOS 上 Virgo 宿主突然消失，例如收到 `SIGKILL` 或进程崩溃，不属于上述已确认清理路径。控制连接消失时，监督进程会尽力请求移除自己的任务，但该请求不能证明所有脱离后代已经退出。这些后代仍受 Seatbelt 限制，但可能继续执行原边界允许的操作；临时资源也可能保留。本阶段不提供资源配额，也不保证任意进程负载下都能在固定时间内完成清理。

宿主 Harness、可信 Python 扩展、项目 hooks、远程 MCP 服务、厂商托管工具或可信集成直接调用底层函数，不处于这层 OS 沙箱内。Core 文件、Web 与 MCP 入口另有应用层检查。可信 MCP 扩展初始化可能在模型调用工具之前启动并连接服务；stdio 服务进程在命令沙箱外运行，也可能自行联网。工具调用门禁不隔离该服务的整个生命周期或 Virgo 全进程。可写工作区仍允许破坏普通项目文件；不提供资源配额或运行中进程的权限撤销。

## 实现与验证

`harness/sandbox.py` 绑定会话策略和调用方，并检查文件工具上限；`tools/sandbox.py` 构建原生边界并检查 POSIX 命令启动确认；Windows 适配器在恢复前验证挂起进程。`tools/macos_process.py` 管理临时 launchd 任务、经过身份验证的描述符传递、真实子进程状态和内核核对的清理。其监督进程是 Seatbelt 外的可信宿主基础设施；命令及其后代仍使用原有 Seatbelt 配置。`tools/windows_sandbox.py`、`windows_sandbox_acl.py`、`windows_sandbox_files.py` 实现 Windows Token、ACL 与原生路径操作，现有 `WindowsProcess` 负责其进程生命周期。现有 Shell 执行、输出记录、取消和后台任务机制继续作为调用方，没有新增 Agent Loop。

`tests/harness/test_sandbox_e2e.py` 定义真实装配会话和进程的验收场景：后端不可用、配置来源、Shell 发现与选择、文件工具限制、硬链接别名、unlink 及路径替换竞争、间接及符号链接写入、自动项目上下文、子任务／后台归属、网络／环境隔离、已有元数据、工作区内虚拟环境、输出位置、实际启动失败、退出码、超时、取消、脱离的后代进程、普通／脱离的 `posix_spawn`、双重 fork 孤儿进程，以及独立后台命令不受其他命令清理影响。原生 CI 设置 `VIRGO_REQUIRE_SANDBOX_TESTS=1`，将后端不可用视为失败，而不是能力跳过。仅通过模拟失败场景不能证明 OS 隔离有效；原生报告与完整跨平台对比报告是分别需要读取的验证结果。

## 三开关组合与验收契约

POSIX 后端为七种受限组合构造独立策略；三个开关完全放开的组合由 No-Limit 显式使用宿主执行。只读保护宿主数据，仍保留私有临时目录与宿主日志。工作区范围包括必要系统／运行时文件，不能解释为解释器完全不读取系统。Windows 为仅工作区组合提供可选联网 capability；需要原生隔离但关闭工作区范围的组合明确返回不支持。Windows 不添加 loopback 豁免，也不改变防火墙设置。

完整宿主可写范围允许修改宿主用户文件及其别名。它保留直接控制路径保护，但不遍历整个宿主查找所有硬链接，因此不具备仅工作区策略的受保护 inode 保证。禁止命令进程树直接联网，也不能撤销宿主文件修改或阻止其他可信宿主程序以后执行这些文件带来的效果。

安全验收必须检查：联网授权未同时放开任意 Unix socket／进程控制；完整宿主只读不会变成可写；文件工具不能通过外部链接绕过工作区范围；改变 cwd 不会换授权根；不支持的组合不能回退为宿主执行。七种 POSIX 组合使用真实进程检查内外文件读写、TCP 与任意 Unix socket 拒绝；独立正向对照用例要求联网策略下实际完成系统 DNS 解析与外部 TCP 连接。Core 文件工具另行检查读取、搜索及修改入口。后端发现或旧提交的通过结果不能替代当前修订的原生 CI。
