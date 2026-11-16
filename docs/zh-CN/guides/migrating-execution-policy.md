# 执行策略迁移

[文档](../README.md) · [English](../../en/guides/migrating-execution-policy.md)

Virgo 现在将工作意图、显式审批和执行边界分别表达。这是一次不兼容改动：旧权限模式报告配置错误，不再在运行期间动态转换。已有对话和计划 Markdown 仍可读取；打开历史不会恢复授权或启动执行。

## 更新配置

| 已退休设置或接口 | 替代方式 |
| --- | --- |
| `permissions.mode=plan`、`/permissions plan` | `workMode=plan`、`--work-mode plan` 或 `/mode plan` |
| `permissions.mode=auto-review`、`default`、`accept-edits` | `permissions.approval=ask`；不确定操作由用户确认 |
| `permissions.mode=accept-all`、`yolo` | 明确选择用户级 allow 规则以及 `ask` 或 `never`；不提供自动等价转换 |
| `permissions.readOnlyCommands` | 删除；Plan 和只读子运行时不提供任意 Shell |
| `plugins.auto_review`、`features.permission` | 删除；Core 授权始终启用，模型不再负责授予权限 |
| `sandbox.network` | 删除；活动 Shell 沙箱固定禁止联网 |
| `--permission-mode ...` | `--approval ask|never`，工作模式另用 `--work-mode default|plan` |
| `--mode text|json` | `--format text|json` |
| `bash(npm test*)` 等 Shell 命令前缀授权 | `bash(npm test)` 等完整命令，或明确授权整个工具 |

`never` 表示需要新增批准的操作会被拒绝。它不授予全部工具权限，不忽略 deny，不退出 Plan，也不关闭 Sandbox。显式拒绝规则始终有效。

将授权和审批策略从项目 `.virgo/settings.json` 移到用户设置。项目 `permissions` 只可添加 deny，并与用户 deny 合并；不能清空用户拒绝、选择审批策略或声明额外只读命令。项目 Sandbox 配置继续被忽略。

普通交互工作的用户配置：

```json
{
  "workMode": "default",
  "permissions": {
    "approval": "ask",
    "allow": ["bash(uv run --no-sync pytest -q)"],
    "deny": ["bash(git push*)"]
  },
  "sandbox": {"mode": "workspace-write"}
}
```

可选的项目限制：

```json
{
  "permissions": {"deny": ["write(secrets/**)", "edit(secrets/**)"]}
}
```

带命令的 Shell allow 规则严格匹配完整文本，命令通配符会被拒绝。文件路径、Web allow 规则和 deny 规则保留 glob 语义。单独的 `bash` 授权整个 Bash 能力，仍受硬边界限制，但可以破坏普通工作区文件；应明确选择这类授权。

## 审批与记住的决定

普通工作区文件操作采用确定性策略；简单本地 Shell 形式可在活动沙箱内自动执行。宿主 Shell、工作区外路径、联网和未知操作需要明确授权或人工批准。原生沙箱不可用不会转为宿主执行；Sandbox 和只读上限会在询问前检查，避免出现批准也不能执行的问题。

人工批准可为单次，或针对来源任务中的准确操作记住。动态 Shell 路径和 PowerShell 脚本只获得单次批准。记住的决定绑定任务身份、工具、参数、cwd、解析后的路径、环境与显式策略。`/permissions revoke` 清除记住的批准和拒绝，改变显式策略也会使旧决定失效。主 Agent 仍接收对话指令，但不再由另一个模型将历史文本解释为授权系统中的授权或撤销。

## 默认执行边界

默认改为 `workspace-write`，minimal 预设也适用。使用 `--sandbox read-only` 施加写入上限；确需宿主执行时显式选择 `--sandbox disabled`。此选择不改变审批策略或 WorkMode。原生边界建立失败仍为错误，命令不会被自动重放。

活动 Shell 沙箱禁止联网，并保护仓库元数据和指令路径。安装依赖、远程 Git 操作和提交可能因此不可用。选择宿主环境前请阅读[沙箱指南](sandbox.md)与[Windows 前提](windows-sandbox.md)。Windows 后端仍属实验支持；管理员 CI 结果不代表普通桌面标准账户可用。Core Python 文件工具使用独立应用层限制；可信扩展、hooks 和远程工具不在 Shell 的 OS 沙箱中。

Plan 和只读子运行时使用文件阅读、搜索和受控 `git_read`，即使显式关闭宿主沙箱也不提供任意 Bash/PowerShell。Code Review 继续作为可选独立任务，使用冻结证据与经过校验的 Findings，不成为第三种工作模式。

## 集成 API

- 审批选择使用 `AgentSession(approval_policy="ask" | "never")` 和 `session.approval_policy`。
- 工作意图使用现有权限服务的 `set_work_mode(...)`。
- 通过 `read_only=True` 创建受限子运行时；可选 `approval_policy="never"` 进一步限制交互审批。子任务不能放宽父级上限。
- 延迟执行必须携带 `ToolContext.origin` / `ToolCallEvent.origin`，由 `ExecutionOrigin` 绑定来源。后台任务发布前就捕获来源，不能借用当前选中的对话。
- `ExecutionOrigin` 保留工作区、来源 Runtime 和分支记账；旧 `ReviewSource` 的额外 transcript、模型审批输入和裁决 API 已删除。
- 自定义权限策略只能给 Core 添加限制，不能替换 Core。旧 reviewer 注册 API 和 `features.permission*` 导入已退休。
- 模式和计划通知在状态提交后发出。监听者可以观察和刷新 UI，不能同步重入状态变更或发布工作；下一次显式操作应在通知结束后走正常准入路径。

切换模式要求来源对话及其排队、运行中的子 Agent 空闲。已经启动的原生进程保留原有执行边界；切换模式不等于撤销进程权限。执行计划为一次运行选择准确版本，并保留当前审批及 Sandbox 设置。编辑计划会使旧执行选择失效；恢复历史不会执行计划。
