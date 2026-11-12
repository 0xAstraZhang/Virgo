# Shell 选择与执行

## 使用

`/shell` 每次重新探测已支持且能成功启动的解释器。全屏界面使用现有底部选择列表，plain 模式使用编号菜单；显示类型、版本、完整路径及当前选项。取消不修改配置；无交互界面仅列出当前配置、可用解释器和用法。

```text
/shell bash
/shell zsh
/shell pwsh
/shell powershell
/shell "C:\Program Files\PowerShell\7\pwsh.exe"
/shell auto
```

支持 Bash、Zsh、POSIX sh、dash、PowerShell 7+ (`pwsh`) 和 Windows PowerShell 5.1 (`powershell`)。CMD、Fish、WSL 发行版和任意自定义解释器尚未适配，不会因为存在可执行文件就展示为可用选项。sh/dash 无通用版本接口时明确显示版本未报告。

探测范围为 PATH 的绝对目录、常见安装目录、Unix `/etc/shells` 中已适配的条目，以及当前选中的路径。每个解释器用固定版本查询做实际启动验证，单次限时 3 秒，并行探测，按类型和实际文件路径去重。额外安装位置可以直接传绝对路径；参数不是 shell 代码，不支持在路径后附加启动参数。

自动选择时 Windows 优先 `pwsh`，其次 Windows PowerShell，再考虑 Bash/sh；macOS/Linux 优先 Bash，其次 Zsh/sh。显式配置失效时关闭命令工具并提示重新选择，避免静默换成另一种语言执行历史命令。

选中后立即生效并保存到用户级 `settings.json` 的 `shell.executable`；`auto` 保存自动选择模式。重启时遵循用户配置 → 项目配置 → Runtime 配置 → `VIRGO_SHELL`，后者优先。`VIRGO_SHELL` 现在只接受已适配解释器的名称或路径。修改默认值保留其他配置字段。

## 模型与会话

- POSIX 解释器沿用 `bash` 工具名，PowerShell 使用 `powershell`，同一个 Runtime 默认只有选中的命令工具。
- 工具描述和系统提示来自同一个不可变 `Shell` 实例，包含类型、实际路径、版本、语法和每次启动独立进程的约定。工具结果 details 也记录解释器信息。
- `/shell` 是非并发命令；前端在当前工作结束后执行它。直接调用接口在主 Agent 忙时拒绝切换。
- 切换更新主 Runtime 工具和系统提示，不修改已有消息；会话记录追加 `shell_change`。这是与权限模式切换类似的显式提示缓存边界。同一选择内仍保持请求前缀稳定。
- 已启动的后台命令和已有子 Runtime 保留原执行器与原系统提示；新 Runtime 使用最新选择。配置没有进程级全局可变的当前 shell。
- 用户自行配置的 hook 命令继续使用原来的执行规则；已有 `bash(...)` 权限规则不会自动扩大到 PowerShell。

## 执行与权限

每次调用关闭 stdin，以实际解释器路径直接启动，不通过父终端或 CMD 转发。Bash/Zsh 使用非交互、无用户 profile 的启动参数，移除 `BASH_ENV`/`ENV`；Bash/Zsh 开启 `errexit` 和 `pipefail`。sh/dash 开启 `errexit`，不宣称具备 `pipefail`。Zsh 的系统级 zshenv 仍受解释器自身规则控制。

PowerShell 使用 `-NoProfile -NonInteractive -OutputFormat Text -EncodedCommand`，载荷为 UTF-16LE Base64。控制台和管道编码设置为 UTF-8，禁用进度输出；不修改执行策略。超出 Windows 命令行长度限制时提示改用 `.ps1` 文件。原生程序自行使用其他输出编码时仍可能乱码。

- cmdlet 错误由 `$ErrorActionPreference='Stop'` 转成失败。
- PowerShell 7.4+ 同时开启 `$PSNativeCommandUseErrorActionPreference`，未处理的原生命令非零状态终止脚本。
- 5.1 / 7.0–7.3 为兼容模式：只自动传播最后一个原生命令退出码。模型必须在每次原生命令后检查 `$LASTEXITCODE`，避免后续成功命令掩盖失败。此限制写入工具描述。
- 预期非零状态应显式处理；PowerShell 中必要时局部关闭 native error 偏好、检查并重置 `$LASTEXITCODE`。`exit N` 保留显式退出码。

前后台复用原有输出截断、日志和任务管理。超时上限 3600 秒；前台默认 300 秒，后台未提供超时则持续运行。Unix 沿用进程组取消；Windows 使用系统 `taskkill /T /F`，失败后至少终止父进程。这是尽力清理，不是 Job Object 隔离，也不保证清理主动脱离进程树的程序。

PowerShell 在 `auto-review` 中按独立规则处理，其余脚本交给审批模型，审批输入包含实际 shell 信息；不使用 POSIX 词法判断。`plan` 不提供 PowerShell，使用文件读取工具调查；`accept-all` 正常执行。动态 PowerShell 路径无法静态确定，不记忆“本会话允许”。审批缓存键包含解释器信息，切换后不复用旧解释器的审批。

## 验证

`tests/test_shell_selection_e2e.py` 使用 faux 模型驱动真实会话、工具与解释器，包含两种 UI 的选择/取消、配置保留和失败回滚、重启优先级、缺失路径、模型元信息、跨会话与后台绑定、中文和引号、工作目录、错误状态、超时、输出截断及终止后代进程。PowerShell 场景在解释器可用时运行；没有安装时跳过。

`.github/workflows/shell-selection.yml` 为 Windows、macOS、Linux 配置相同端到端入口；本机通过不等价于 Windows CI 已通过。

参考：[PowerShell 启动参数](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh?view=powershell-7.5)、[错误偏好变量](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_preference_variables?view=powershell-7.5)。
