# 迁移权限设置

在 `~/.virgo/settings.json`（或 `$VIRGO_HOME/settings.json`）中设置 `execution.mode`。例如，要继续使用可写工作区沙箱：

```json
{"execution": {"mode": "workspace-edit"}}
```

已有用户设置文件且未选择执行模式时，沿用旧版 Workspace-Edit 默认值，包括只有空 JSON 对象、模型或语言设置的文件。若仅保存了自定义预设，Virgo 按旧版实际生效的 Workspace-Edit 预设迁移。旧配置中明确指定的 `sandbox.mode=disabled` 对应 No-Limit。

用户设置文件不存在时，Virgo 默认使用 Workspace-Edit，包括仅有项目设置的情况。新版首次保存普通用户设置时会一并记录此默认模式。命令行临时选择的模式仅对本次启动生效。

可选值为 `read-only`、`workspace-edit` 和 `no-limit`。命令行参数 `--permission` 为本次启动选择模式。模式和持久审批由用户管理，项目配置无权设置。

迁移旧用户预设时，Virgo 同时核对文件权限、受管网页访问和 Shell 联网。可写工作区且允许联网的旧模式对应 Workspace-Edit，无限制执行对应 No-Limit。旧离线预设需要用户显式选择，因为两个受限模式现在均允许受管网页访问。允许 Shell 联网的旧只读预设也需要用户选择，因为新 Read-Only 默认关闭 Shell 联网。旧配置中非空的命令 allow/deny 列表需要手动迁移。[模式选择与持久审批](../development/permission-profiles.md)
