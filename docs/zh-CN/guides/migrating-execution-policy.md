# 执行策略迁移

[文档](../README.md) · [English](../../en/guides/migrating-execution-policy.md)

任务指令与执行权限现在独立。Plan 和 Code Review 是拥有正常工具的可选插件，通过提示词
要求输出计划／报告，不再附加只读权限。已有对话和计划仍然可读。

## 更新配置

| 原入口 | 当前入口 |
| --- | --- |
| `/mode plan` / `/mode default` | `/plan` / `/plan off` |
| `workMode=plan`、`--work-mode plan` | 改用 `plan: true` / `--plan`；Plan 插件启用时选择规划指令 |
| `/mode` | `/permissions` 的弃用别名；查看／选择可编辑的**权限**预设 |
| `--mode text|json` | `--format text|json`；旧 `--mode` 预设写法是 `--permission` 的弃用别名 |
| `sandbox.mode=workspace-write` / `read-only` | 显式保留旧禁网策略；删除后使用新默认值 |
| `sandbox.mode=disabled` | 显式选择 No-Limit 文件／网络权限 |
| `sandbox.network` | 改用预设的 `networkAllowed` 布尔值 |
| `permissions.mode=plan|default|accept-edits|accept-all|yolo|auto-review` | 旧审批别名已删除；`permissions.preset` 选择 `read-only`、`workspace-edit`、`no-limit` 或自定义 ID |
| `permissions.readOnlyCommands`、`plugins.auto_review` | 已删除；Shell 字符串分类和模型裁决不构成执行边界 |

新默认值为 `permissions.preset=workspace-edit`，允许编辑工作区及任务联网。Read-Only 也允许
联网；离线工作通过自定义三开关预设表达。名称与权限可独立编辑。完整 JSON 及 `/permissions save`
语法见[配置](../reference/configuration.md#权限)。权限预设和授权由用户设置管理，项目只能增加 deny。

## 审批与记住的决定

所选权限范围内的受管操作直接执行。`request_permissions` 为一个准确操作申请明确的权限
变化；批准本身不执行操作，下一次相同工具调用消费例外，Mode 不变。不同参数、来源 Runtime／
运行、解析路径、Shell 环境、策略版本或工具实例不能复用它。

`ask` 允许交互申请，`never` 拒绝新增申请，而非拒绝全部操作。`/permissions revoke` 清除
记住的决定和待消费例外。显式 deny 始终生效。失败或可能已有副作用的命令不会自动重放。
打开历史不恢复授权，也不执行计划。可信扩展策略可以增加限制。

## 默认执行边界

每次运行捕获来源权限。运行中切换预设用于后续运行；后台任务及子 Runtime 保留原来源。
只读保护宿主文件数据，私有临时目录和宿主记录仍可写，也不保证远程系统没有修改。

Core 文件工具在应用层执行文件范围检查；原生 Shell 后端执行支持的进程策略；受管 Web／
原生搜索和远程 MCP 调用遵循网络开关。无法执行文件边界的 MCP 服务，需要明确的单次不限
文件范围例外。宿主和可信 Python 扩展不在 Shell 的 OS 边界中。

参见[沙箱](sandbox.md)与[Windows 限制](windows-sandbox.md)。Windows 保留系统 LPAC 读取
授权，并拒绝受限的 `workspaceOnly=false` 组合。发现后端不代表已成功隔离；不支持或不可用
会拒绝执行。No-Limit 使用宿主账户已有权限，不自动取得管理员权限。

## 集成 API

`SandboxManager.mode_for(runtime)` 返回捕获的 `PermissionMode`；`selected_mode` 是当前
对话下次采用的选择。`select_mode(id)` 只切换当前对话，`save_mode(...)` 将名称与开关写入
用户设置。实际判断使用三个有效值。`Runtime.prepare_run()` 捕获策略。

`read_only=True` 仍可明确限制子 Runtime，但不移除工具 schema；预设继承与该显式上限分开。
延迟工作携带 `ToolContext.origin`，TUI 切换对话不会转移授权。存储的历史记录不会恢复单次例外。

Plan 通过 `plugins.plan` 安装，Core 不再必然拥有 Plan 服务。扩展使用
`add_request_system_section` 注入每请求指令，复用既有 Runtime 和 SessionStore。
Review 保留目标解析、冻结证据与 Findings 校验，同时使用正常工具和交互路径。
`harness.plans` 导入仅作为兼容入口，新集成使用插件 API。
