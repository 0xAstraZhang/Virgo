# 权限模式迁移

使用 `--permission read-only`、`--permission workspace-edit` 或 `--permission no-limit`。新安装默认 No-Limit。兼容参数 `--execution host` 选择 No-Limit。CLI 与 TUI 提供相同的三种模式。

迁移时比较原来的文件、网页和 Shell 网络权限。没有固定模式能同时保留三者时，Virgo 返回配置错误。旧离线工作区需要用户显式选择，因为 Workspace-Edit 开放 Shell 联网，两个沙箱模式都允许网页访问。

`/permissions show` 显示当前任务和下一任务的模式，`/permissions list` 列出模式。使用 `/permissions grants` 查看持久授权，`/permissions revoke ID` 撤销指定授权。项目没有授予执行权限的入口。运行中的子任务与后台操作保持已捕获的上下文。

详见[权限模式](../development/permission-profiles.md)及[原生后端](../guides/sandbox.md)。
