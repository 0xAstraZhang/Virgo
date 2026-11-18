# 权限模式与执行

| 模式 | 文件权限 | 公共网页工具 | Shell 联网 | 执行后端 |
| --- | --- | --- | --- | --- |
| Read-Only | 可读项目及所需运行时文件，命令有私有临时目录 | 允许 | 按操作申请 | 原生沙箱 |
| Workspace-Edit | 可读写真实工作区，Git 元数据及 Virgo 私有状态等路径受保护 | 允许 | 允许 | 原生沙箱 |
| No-Limit | 使用宿主账户权限 | 允许 | 允许 | Host |

首次启动默认 No-Limit。可在用户设置中指定 `execution.mode`，或通过 `--permission read-only`、`--permission workspace-edit`、`--permission no-limit` 启动。旧参数 `--execution host` 对应 No-Limit。`/permissions list` 和 TUI 的 Shift+Tab 列出三个模式。`/permissions <id>` 切换下一任务的模式，`/permissions show` 显示当前任务及下一任务的选择。

Core 在接纳任务时固定来源工作区和执行上下文。排队的子任务继承提交时的上下文，运行中的任务在切换模式后继续使用原权限。子任务的 `approval_policy="never"` 对其后代同样生效。文件工具和 Shell 直接访问同一工作区。No-Limit 直接使用 Host。选用沙箱时才加载平台后端，无法实施请求的文件或网络边界时停止操作。[各平台支持范围及实时文件系统约定](../guides/sandbox.md)

Core 根据本次操作判断缺少的权限。文件读写超出范围时，Virgo 在执行前直接询问用户。Shell 可在执行前通过 `request_permissions` 同时申请额外的路径和联网权限。审批框列出具体操作、工作目录、新增资源和原因。

- **仅允许一次**绑定当前任务的确切操作，执行时消费。显式申请后尚未使用的一次批准在五分钟后或本轮任务结束时失效。
- **始终允许**将操作及资源保存在用户目录的 `resource-grants.sqlite3`。匹配时检查项目、模式、工作目录、工具和完整参数，Shell 操作还检查解释器身份。已有目录读取权限的同一命令再次申请写入时，只询问写权限。
- **拒绝**保留原权限。已经启动的进程继续使用启动时取得的权限。

`/permissions grants` 查看当前项目的持久授权，`/permissions revoke ID` 撤销指定记录。TUI 模式选择器也提供「已保存的授权」入口。项目配置无权添加批准记录。[配置参考](../reference/configuration.md) · [旧配置迁移](../guides/migrating-execution-policy.md)
