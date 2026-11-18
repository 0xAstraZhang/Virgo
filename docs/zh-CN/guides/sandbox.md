# 原生命令沙箱

No-Limit 直接使用当前账户的 Host 权限。Read-Only 和 Workspace-Edit 在选用时加载 Sandbox 扩展。两者均允许网页搜索与读取。Read-Only 默认关闭 Shell 网络，Workspace-Edit 允许 Shell 联网。详见[模式与审批](../development/permission-profiles.md)。

## 实时工作区

命令、文件工具和宿主编辑器访问同一目录。文件工具保留打开对象的身份，写入新文件并替换目标。编辑器和运行中的监听器可以立即重新打开该路径读取结果。每条命令拥有私有临时目录和 HOME。Core 持有来源工作区中的输出句柄。

Linux 使用 Bubblewrap 和 libseccomp，macOS 使用 Seatbelt 和 launchd coalition 进程监督，Windows 使用 LPAC、受限令牌、临时 ACL 和 Job Object。平台建立边界失败时向调用者返回原因。已启动的进程保留启动时获得的资源。

## 文件保护

工作区内已有的控制文件和嵌套解释器目录受写保护。POSIX 启动检查会拒绝含有多重硬链接的可写文件。实时工作区的支持前提是宿主写入者在命令运行期间维持路径的权限边界。宿主并发改变链接关系可以改变路径所指对象，启动检查无法持续验证这种变更。额外的 Shell 路径必须是确实存在的文件或目录，单个文件的授权仅覆盖该文件。

Broker 和可信扩展使用各自的操作系统权限。发布验收以最终提交的原生测试和完整跨平台比较为准。见[验收记录](../development/execution-acceptance.md)和 [Windows 平台范围](windows-sandbox.md)。
