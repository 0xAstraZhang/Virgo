# 命令解释器

[文档首页](../README.md) · [English](../../en/guides/shells.md)

## 选择 Shell

Virgo 在启动时探测受支持的解释器。`/shell` 显示缓存的选项，包括可执行文件路径和报告的版本；每次打开选择器不会重新扫描整台机器。

```text
/shell bash
/shell zsh
/shell pwsh
/shell powershell
/shell "C:\Program Files\PowerShell\7\pwsh.exe"
/shell auto
```

支持 Bash、Zsh、POSIX sh、dash、PowerShell 7+ 和 Windows PowerShell 5.1。CMD、Fish、WSL 发行版及任意自定义解释器没有对应适配器。选中的名称或路径会通过实际启动验证，不能附带额外启动参数。新安装的解释器可以直接指定；重启会刷新完整列表。

Windows 自动优先选择 PowerShell 7，再选择 Windows PowerShell；macOS/Linux 优先 Bash，再选择 Zsh/sh。显式配置失效时会停用命令执行，并提示重新选择，不会静默改用另一种语言。

选择立即应用于当前会话，并将 `shell.executable` 保存到用户配置。重启时项目和运行时配置可以覆盖它，`VIRGO_SHELL` 优先级最高。取消或验证失败会保留原选择。

## 执行语义

POSIX 解释器沿用 `bash` 工具名，PowerShell 使用 `powershell`。模型会获得实际解释器的类型、版本、路径和语法信息。每条命令都启动独立进程并关闭标准输入，`cd` 等 Shell 状态不会跨调用保留。

| Shell | 错误处理与启动行为 |
| --- | --- |
| Bash／Zsh | 非交互、无用户 profile，开启 `errexit` 和 `pipefail`，移除 `BASH_ENV`／`ENV` |
| sh／dash | 非交互并开启 `errexit`，不保证具有通用 `pipefail` |
| PowerShell 7.4+ | `-NoProfile`、非交互、UTF-8 输出；cmdlet 错误和未处理的原生命令失败会停止执行 |
| Windows PowerShell 5.1／PowerShell 7.0–7.3 | cmdlet 错误停止执行；只自动传播最后一个原生命令退出码，较早命令需显式检查 `$LASTEXITCODE` |

Zsh 仍遵守系统级启动规则。PowerShell 使用编码命令，不修改执行策略；超过 Windows 命令行长度上限时需要改用脚本文件。预期的非零退出码需要在脚本中显式处理。

`/shell` 等待当前主任务结束后执行。已启动的后台任务和子 Runtime 保留原解释器，新 Runtime 使用新选择。用户配置的命令钩子继续遵守自身的执行规则。

## 输出、超时与取消

前台和后台的 stdout/stderr 都直接写入 `.virgo/outputs/` 下的文件，命令未结束时即可读取。显示内容是有界的文件尾部；结果提供 `output_path`、`output_bytes` 和 `truncated`。完整日志保留原始字节，显示时按 UTF-8 解码并替换非法字节。日志创建失败时不会启动进程。

前台命令默认超时 300 秒，上限为 3600 秒。后台命令没有显式超时时，可以运行至完成或取消。Unix 使用进程组取消；Windows 10+/Server 2016+ 使用 Job Object 管理进程树，在超时、取消、完成或会话关闭时清理后代进程。Job Object 管理进程生命周期，不是权限沙箱。

## 权限

PowerShell 使用独立的 `powershell(...)` 规则，已有 `bash(...)` 规则不会自动授权它。自动审查会接收实际 Shell 信息。计划模式不提供 PowerShell，使用文件工具调查。动态 PowerShell 路径不能可靠缓存为“本会话允许”。更换解释器后，不会复用另一解释器的审批缓存。

配置与平台验收范围见[权限模式](../reference/configuration.md#权限)和[开发检查](../development/testing-and-ci.md)。

实现：[Shell 适配器](../../../src/virgo/tools/shell.py)、[Shell 选择](../../../src/virgo/harness/shell_commands.py)、[Windows 进程管理](../../../src/virgo/tools/windows_process.py)。
