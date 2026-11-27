# 执行边界与沙箱

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/guides/sandbox.md)

> Virgo 的沙箱是防止模型偶发误操作的**护栏**：它把写入限制在当前工作区内，保护高敏凭据与 Virgo 项目配置，并在越界或高危操作前请求确认。它**不是**抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行 Virgo。

Linux、macOS、已更新的新版 Windows 11 与 Windows + WSL2 均为**一级支持**平台，默认以 **Workspace-Edit** 启动。不支持非提权标准 AppContainer 的旧版 Windows 默认以 **No-Limit** 启动并给出一次性提示（**1. WSL2**、**2. No-Limit** 或 **3. 更新 Windows**）。平台细节见[原生 Windows 沙箱](windows-sandbox.md)与 [WSL2 使用指南](wsl2.md)。

## 护栏覆盖的目标

1. **工作区范围与 `.virgo` 写时确认**：**Workspace-Edit** 默认允许直接读写当前项目工作区（包括 `.git`、`AGENTS.md`、`CLAUDE.md` 与项目 `.venv`，使日常 Git 操作、文档更新与装包全程无阻）；修改项目 `.virgo`（`PROTECTED_NAMES = (".virgo",)`）时触发确认弹窗（未获批前在 Shell 中保持只读保护，macOS Seatbelt 下即使 `.virgo` 尚不存在也禁止创建）。Broker 文件工具（`read`、`write`、`edit`、`glob`、`grep`、`present_file`）直接在应用层按 `PermissionPolicy` 校验（`read`/`write`/`edit`/`present_file` 按解析后的精确文件路径确认，`glob`/`grep` 按目录前缀确认），并使用末级 `O_NOFOLLOW` 与原子临时文件替换。
2. **高敏凭据与环境变量保护**：Linux、macOS 与原生 Windows 上 Virgo 的私有目录（`~/.virgo` 或 `$VIRGO_HOME`）及高敏凭据路径（`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.kube`、`~/.azure`、`~/.config/gcloud`、`~/.config/gh`、`~/.netrc`、`~/.npmrc`、`~/.pypirc`、`~/.git-credentials`、`~/.docker/config.json`）均硬性拒绝读写，且不可通过 `request_permissions` 申请放行。沙箱 Shell 自动过滤凭据型与注入型环境变量（如 `*_API_KEY`、`*_SECRET` / `*_SECRET_*`、`*_TOKEN`、`*_PASSWORD`、`OPENAI_*`、`ANTHROPIC_*`、`AWS_*`、`GITHUB_TOKEN`、`SSH_AUTH_SOCK`、`VIRGO_HOME`、`PYTHONPATH`、`BASH_ENV`、`LD_PRELOAD` 等），同时保留 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` / `ALL_PROXY`、`SSL_CERT_FILE` 及语言工具链变量，并提供会话内复用的包缓存目录（`XDG_CACHE_HOME`、`UV_CACHE_DIR`、`PIP_CACHE_DIR`、`NPM_CONFIG_CACHE`）。**Read-Only** 默认关闭 Shell 网络，**Workspace-Edit** 允许 Shell 出站联网。
3. **高危命令逐次确认**：不可逆或高代价的 Shell 命令（`git push --force`、推送至受保护分支、`git reset --hard`、`git clean -f`、`curl | sh`、工作区外递归删除、部署/发布、云资源删除，以及通过数据库/缓存 CLI 执行的破坏性 SQL/Redis 语句）在 **Read-Only** 与 **Workspace-Edit** 模式下均要求显式一次性确认（在 **No-Limit** 模式下默认关闭，可通过 `permissions.confirmHighRisk: true` 开启）。
4. **失败可见，不悄悄降级**：原生沙箱后端无法建立时，Virgo 明确报错，在交互式会话中提供显式的“本次使用 No-Limit”选项（不改变会话模式），绝不静默退回 Host，也绝不自动重放可能已经启动的命令。

## 不在范围内的目标

- 抵御主动试图绕过护栏的模型。
- 抵御精心构造的攻击链（TOCTOU 竞态、预埋软硬链、大小写变形、恶意依赖、恶意并发宿主写入者或操作系统/内核漏洞）。
- 承诺防住提示词注入攻击链（但硬屏蔽私有凭据、`.virgo` 写时确认、不可信项目配置剥离和 Read-Only 断网仍能缓解常见误伤与注入影响）。
- 要求所有操作系统的隔离强度完全一致。

## 沙箱 Shell 命令的临时文件

- 在 Linux、WSL2 和 macOS 上，宿主的 `/tmp` 默认只读。临时文件应写入 `$TMPDIR`。`TMPDIR`、`TMP` 和 `TEMP` 都指向同一个可写目录，Virgo 会在该命令结束时删除整个目录。
- 在原生 Windows 上，单条命令使用的临时文件应写入 `TMPDIR`（PowerShell 中的 `$env:TMPDIR`），Virgo 会在命令结束时清理该目录。`TEMP` 和 `TMP` 指向 AppContainer 会话共享的临时目录（同一会话中后续命令仍需使用的临时文件可写入 PowerShell 的 `$env:TEMP` 或 `cmd` 的 `%TEMP%`）。

只有需要跨调用保留的临时文件才应写入工作区内有写权限的位置，并在最后一次使用后删除。

## 各平台后端与如实说明的差异

- **Linux（一级 — `bubblewrap`）**：仅需系统 `bwrap` 与非特权用户命名空间（`--unshare-all --unshare-user --disable-userns --cap-drop ALL`，通过 `--info-fd` 完成启动确认，无需 `libseccomp` 运行时依赖）。以只读方式挂载宿主常规路径（`--ro-bind / /` 与真实只读 `$HOME`），确保用户态工具链（`~/.cargo`、`~/.rustup`、`~/.local`、`nvm`、`pyenv` 等）、本地 `AF_UNIX` 套接字、`AF_NETLINK` 与 `io_uring` 正常工作；对高敏凭据路径用权限 `000` 的 `tmpfs` 或空只读文件覆盖屏蔽，并对工作区、会话缓存和单命令 `TMPDIR` 挂载可写层。由于 `/run` 以隔离的空 `tmpfs` 挂载，宿主机 D-Bus、systemd、`$XDG_RUNTIME_DIR` 下的桌面/凭据会话服务以及宿主机守护进程套接字（如 `/run/docker.sock`）在沙箱 Shell 内不可达。已存在且未获批的 `.virgo` 通过只读 bind 挂载保护；尚不存在的 `.virgo` 在 Shell 内新建属于 Linux bind 挂载的固有限制，Broker 文件工具与不可信项目配置剥离仍对其进行保护。
- **macOS（一级 — Seatbelt）**：每个执行器首次启动时探测一次 `/usr/bin/sandbox-exec`，随后在独立 POSIX 进程组中直接拉起子进程（`HostProcess` 通过 `waitid(WNOWAIT)` / `kqueue` + `os.killpg` 清理同进程组子进程树）。允许只读访问宿主常规路径与真实只读 `$HOME`，在工作区、会话缓存和单命令 `TMPDIR` 内放行写入与本地 `AF_UNIX` 套接字，对未获批的 `.virgo`（无论是否已存在）实施写拒绝，并对全部高敏凭据路径强制读写拒绝。默认拒绝宿主机 Mach/XPC 服务（仅放行基础目录解析服务如 `com.apple.system.opendirectoryd.libinfo`），因此依赖宿主机守护进程的工具（如 Spotlight `mdls`/`mdfind`、Keychain、剪贴板 `pbcopy`/`pbpaste`、`open`、`osascript`）在沙箱内会失败，且可能表现为通用错误（如 `could not find`）；读取本地文件信息时优先直接解析文件本身。显式调用 `setsid()` 或脱离进程组的后台进程不会被自动回收（与 POSIX Host 命令一致）。
- **原生 Windows（一级 — 标准 AppContainer `windows-appcontainer`）**：支持本地 NTFS 与 Windows 11 ReFS Dev Drive 固定卷，无需管理员权限；复用会话级单 AppContainer SID 与单次 OS 传播的可继承 ACE，自动为 `.venv` 底层 Python 及用户自有 `PATH` 工具链补充只读授权，配合 Job Object（`KILL_ON_JOB_CLOSE`）进程树清理、在祖先目录（如 `C:\Users`）缺少 `S-1-15-2-1` 时映射会话内 `DefineDosDeviceW` 虚拟盘符，以及带自动启动崩溃恢复的预写 JSON 日志。已存在且未获批的 `.virgo` 通过 `protect_readonly_path()`（`SE_DACL_PROTECTED` + 显式 `ALLOW RX`）保护，会话中途获批写权限时自动恢复可写继承。宿主机桌面与凭据服务受 AppContainer 隔离，私有用户目录由默认 NTFS ACL 拒绝读写（不支持以 `$HOME` 作为工作区），且**本机回环（`127.0.0.1` / `[::1]`）在非管理员 AppContainer 下仍被阻止**。运行条件与限制详见[原生 Windows 沙箱](windows-sandbox.md)。
- **Windows + WSL2（一级）**：在 WSL2 中直接使用 Linux `bubblewrap` 后端；适合旧版 Windows、Linux 工具链或需要沙箱内访问本机回环服务的场景。见 [WSL2 使用指南](wsl2.md)。

## Broker 工具、输出存储与可信集成

- **文件工具**由 Broker 直接按 `PermissionPolicy` 执行（不启动独立的 OS 沙箱辅助进程），保证冷启动在 5 秒内、单次 `read` 开销不随工作区规模增长。
- **命令输出**通过管道写入 Broker 管理的会话级持久 `OutputStore`（位于 `<VIRGO_HOME>/outputs/session-<id>`），恢复历史会话后仍可通过 `read_output` 读取，随会话删除或 7 天 TTL 清理；未截断的前台命令不残留文件。
- **用户可信集成**（Python 扩展、命令 hooks、stdio MCP 服务器）以 **Host** 权限运行；信任边界在安装或显式信任项目（`extensions.trustedProjects` / `--trust-project-extensions`）。对于 URL 类型的 MCP 服务器，Host 客户端连接到配置的端点，工具由服务端执行。Virgo 在合并不可信项目的配置前会剥离 `hooks`、`mcpServers`、`providers`、`extensions`、`shell`、`search` 和 `fetch`。扩展和 MCP 加载器也会跳过该项目的 `.virgo/extensions/` 和 `.virgo/mcp.json`。个人 Shell、网页工具后端和 API 密钥设置可写在 `~/.virgo/settings.json`。

另见：[权限模式与审批](permissions.md) · [原生 Windows 沙箱](windows-sandbox.md) · [WSL2 使用指南](wsl2.md) · [原生执行与性能验收](../development/execution-acceptance.md) · [决策 001](../decisions/001-guardrail-threat-model.md)
