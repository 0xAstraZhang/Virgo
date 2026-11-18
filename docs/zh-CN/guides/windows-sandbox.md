# Windows 命令沙箱

原生 Windows 后端要求 x64 Python、PowerShell 7、本地 NTFS，以及创建 AppContainer 配置和临时 ACL 的权限。Virgo 在释放挂起的进程前核验其 LPAC 令牌和 Job 成员资格。进程树结束后收回临时授权。Windows 已授予 LPAC 的环境读取权限仍然生效。

Workspace-Edit 直接请求 Internet 和专用网络能力。Read-Only 在某次操作获准联网前保持关闭。实际出站、入站和宿主回环连接由 AppContainer 与宿主防火墙控制。防火墙规则和回环豁免由宿主管理。

额外授权按确切文件对象或指定目录树应用。目标不存在或无法设置精确 ACL 时返回错误。Virgo 使用当前账户执行。

Windows 专项工作流要求零失败、零错误、零跳过。记录提交、系统、账户类别，以及出站、入站、宿主回环各自的观察结果。Windows Server 2022/2025 管理员运行结果仅覆盖各自环境，普通桌面账户需要单独实测。见[原生验收](../development/execution-acceptance.md)。

Windows 11 build 26200、x64 Python 3.13.9、未提升权限的桌面账户实测中，实时读取正向用例在载荷启动前被拒绝。创建 KsecDD 设备受保护的协调互斥对象返回 Windows 错误 1307。现有 LPAC 实现依赖这一租约。该账户的出站、入站与回环均未测得。
