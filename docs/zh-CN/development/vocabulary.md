# 术语与词汇

[架构总览](architecture.md) · [Core 与插件边界](core-plugin-boundaries.md) · [English](../../en/development/vocabulary.md)

Virgo 有三个彼此独立的轴。代码、配置、命令行、斜杠命令和文档对每个轴只使用一个词。同义词是迁移别名，不是第二个概念。

## 三个轴

| 轴 | 规范词 | 决定什么 | 出现位置 |
| --- | --- | --- | --- |
| 能力 | **capability profile**（配合 **plugins**） | 会话里有哪些可选工具和命令 | `profile`、`plugins.<id>.enabled`、`--profile` |
| 权限 | **permission preset**（配合 **approval**） | 受管的文件、Shell 和网络操作能否执行 | `permissions.preset`、`permissions.presets`、`permissions.approval`、`--permission`、`--approval`、`/permissions` |
| 任务 | **Plan** | 模型是否收到规划指令 | `plan`、`--plan`、`/plan` |

**代码审查** 是一次操作（`/review`），不是第四个轴，也不是持久状态。它在独立运行时中执行。

## 规范术语

| 术语 | 含义 | 不要叫 |
| --- | --- | --- |
| `profile` | 会话构造时选择的能力集：`coding` 或 `minimal` | 与权限预设同时出现时叫 "capability preset" |
| `plugins.<id>` | profile 内的显式能力开关 | `features.<id>` |
| permission preset | 由 `readOnly`、`workspaceOnly`、`networkAllowed` 三个开关和稳定 ID 组成 | "mode"、"sandbox mode"、"permission Mode" |
| approval | `ask` 或 `never`：是否允许申请新的单次操作批准 | "auto review"、"accept-edits"、"yolo" |
| Plan | 可选的规划任务；`/plan off` 退出 | "work mode"、"workflow"、"task type" |
| review | 一次独立的代码审查运行 | "review mode" |
| sandbox | 权限预设背后的原生 OS 边界 | 权限预设本身 |
| session | 一份持久化的对话及其存储 | "pane"、"tab" |
| conversation | 面板中显示的一个活动运行时 | 需要指存储文件时不要用 "session" |

单独说 `preset` 时永远指权限预设；单独说 `profile` 时永远指能力集。两者都不要叫 "mode"。

## 命令与参数

| 意图 | 规范入口 | 弃用别名（保留一个版本） |
| --- | --- | --- |
| 查看或修改权限设置 | `/permissions [preset-id\|list\|save <id> <JSON>\|ask\|never\|revoke\|show]` | `/mode`、`/sandbox` |
| 选择启动时的权限预设 | `--permission <preset-id>` | `--mode` |
| 选择审批策略 | `--approval ask\|never` | — |
| 查看后端可用性 | `/permissions show` | `/sandbox` |
| 进入、查看、编辑、执行或退出 Plan | `/plan [goal\|show\|edit [Markdown]\|execute <id>:<revision>\|off]` | `/workflow default\|plan` |
| 以 Plan 启动 | `--plan` | `--work-mode plan` |

弃用别名会打印提示；它们不改变规范入口的含义，并会在后续版本移除。

## 状态归属

- `SandboxManager` 拥有当前权限预设，并把它映射到平台后端。
- `Permissions` 拥有已记住的批准和拒绝，并对单次工具调用做确定性判断。
- `Runtime.permission_snapshot` 在 `prepare_run()` 时一次性捕获预设、审批策略和修订号。
  运行中切换预设只影响下一次运行；正在运行的快照保持不变。
- 子运行时继承父快照。显式只读子任务是上限，不是新的预设。
- Plan 状态属于对话。store 记录 `plan` 和 `proposed_plan`；旧的 `work_mode` 记录会被读取，但不会自动改写。
- `Runtime.work_mode` 是 0.4 前的内部兼容访问器；新代码读取 `Runtime.permission_snapshot` 或 Plan 记录。

## 配置迁移

新配置使用：

```json
{
  "profile": "coding",
  "plan": false,
  "permissions": {
    "preset": "workspace-edit",
    "presets": {},
    "approval": "ask",
    "deny": ["bash(git push*)"]
  }
}
```

以下旧键会被读取并提示，但不会自动写回：

| 旧 | 新 |
| --- | --- |
| `permissions.mode` | `permissions.preset` |
| `permissions.modes` | `permissions.presets` |
| `workMode: "plan"` | `plan: true` |
| `--mode <id>` | `--permission <id>` |
| `--work-mode plan` | `--plan` |
| `sandbox.mode` | 具有相同开关的权限预设 |
| `features.<id>` | `plugins.<id>` |

项目设置只能增加 `permissions.deny`，不能选择预设或设置审批策略。升级到 0.4 前请删除遗留键。

## 新代码的命名规则

1. 一个状态只有一个所有者。同一决定的第二份数据只能是快照或派生视图，不能是另一个字段。
2. Core 不点名插件工具或命令。用 `AgentTool` 元数据或注册策略表达行为。
3. 插件使用 `ExtensionAPI`，不要给 `Runtime` 写未声明属性，也不要访问 `session._lock`。
4. 命令通过 `UI.command_result` 返回用户可见输出（或返回结果对象）；普通状态文本用 `UI.print`。
5. 本页没有的术语，不要加入配置、CLI、斜杠命令或用户文档。
