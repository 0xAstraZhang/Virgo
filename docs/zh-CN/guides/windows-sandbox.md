# Windows 命令沙箱

原生 Windows 后端要求 x64 Python、PowerShell 7、本地 NTFS，以及创建 AppContainer 配置和临时 ACL 的权限。Virgo 在释放挂起的进程前核验其 LPAC 令牌和 Job 成员资格。进程树结束后收回临时授权。Windows 已授予 LPAC 的环境读取权限仍然生效。

Workspace-Edit 直接请求 Internet 和专用网络能力。Read-Only 在某次操作获准联网前保持关闭。实际出站、入站和宿主回环连接由 AppContainer 与宿主防火墙控制。防火墙规则和回环豁免由宿主管理。

额外授权按确切文件对象或指定目录树应用。目标不存在或无法设置精确 ACL 时返回错误。Virgo 使用当前账户执行。

内置文件工具通过可信 Helper 副本进入相同的原生边界。原子 Write/Edit 在目标目录内暂存文件，因此需要明确的目录写权限。替换后的文件继承父目录 ACL，不复制旧文件自定义的显式 DACL。Helper 使用不跟随 reparse point 的保留句柄和原子原生重命名，不取得 Broker 的 ACL 修改权限。详见[Helper 协议与输出权限](../development/filesystem-helper.md)。

在提交 `37d4427` 上，Windows Server 2022/2025 管理员运行器成功建立外部 TCP 连接。宿主回环和宿主接口连接均未成功。入站套接字成功绑定并监听，但宿主端连接超时。[Windows 原生报告](https://github.com/JunhangYU1017/Virgo/actions/runs/44270022381) · [验收方法](../development/execution-acceptance.md)

Windows 11 build 26200、x64 Python 3.13.9、未提升权限的桌面账户实测中，实时读取正向用例在载荷启动前被拒绝。创建 KsecDD 设备受保护的协调互斥对象返回 Windows 错误 1307。现有 LPAC 实现依赖这一租约。该账户的出站、入站与回环均未测得。
