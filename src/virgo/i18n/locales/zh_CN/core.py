"""Language selection and shared UI messages."""

MESSAGES = {
    "language.description": "查看或切换界面语言",
    "language.usage": "用法：/language [{languages}]",
    "language.current": "当前语言：{language}。{usage}",
    "language.changed": "语言已切换为{language}，并保存为默认设置；重启时项目和运行参数中的设置优先。",
    "language.invalid": "不支持的语言：{value}。{usage}",
    "language.busy": "智能体正在运行，完成后才能切换语言。",
    "language.save_failed": "无法保存偏好设置，语言未更改：{error}",
    "common.unavailable": "不可用",
    "common.cancelled": "已取消。",
    "ui.base.back": "返回",
    "ui.base.multiline_help": "单独输入 . 结束；/cancel 取消；直接输入 . 保留现有内容。",
    "sandbox.description": "查看原生命令沙箱状态",
    "sandbox.status": "沙箱配置：{mode}；当前有效边界：{effective}；后端：{backend}",
    "sandbox.backend_unverified": "已发现 {backend}；尚未验证本次启动（每次启动均检查）",
    "sandbox.backend_unavailable": "不可用：{reason}",
    "sandbox.host_execution": "宿主执行；无 OS 隔离",
    "sandbox.scope": "原生隔离覆盖 Core Shell 进程。Core 文件读写及受管 Web/MCP 调用通过应用层检查遵循三个开关；宿主记录和可信 Python 扩展不在 OS 边界中。已运行任务保留捕获的权限。",
    "sandbox.windows_scope": "实验性 Windows 支持需要原生 x64 LPAC、NTFS 和 PowerShell 7。普通账户和桌面 Windows 尚未验证。读取范围包含系统已有的 LPAC ACL 授权；独立令牌限制写入。每次启动都会检查可用性。",
    "sandbox.file_denied": "沙箱禁止修改文件：{path}",
    "sandbox.cli": "旧禁网沙箱兼容选项；可编辑权限预设请使用 --mode",
}
