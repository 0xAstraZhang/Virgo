# 配置参考

[文档首页](../README.md) · [English](../../en/reference/configuration.md)

## 文件与优先级

Virgo 按以下顺序深合并内置默认值、用户配置和项目配置：

1. [`harness/settings.py`](../../../src/virgo/harness/settings.py) 中的默认值。
2. `~/.virgo/settings.json`，或 `$VIRGO_HOME/settings.json`。
3. `<工作目录>/.virgo/settings.json`。
4. 支持的环境变量和运行时／CLI 覆盖值。

嵌套对象合并，高优先级值覆盖低优先级值。项目配置只能追加 deny 规则，授权和审批策略必须来自用户配置或显式启动参数。CLI 指定的模型覆盖 `VIRGO_MODEL`，后者覆盖配置文件。`VIRGO_SHELL` 覆盖用户、项目和运行时指定的解释器。命令保存用户默认值时会保留无关字段；下次启动时，项目或启动参数仍可能优先。当 `<工作目录>/.virgo` 与用户配置目录（`~/.virgo` 或 `$VIRGO_HOME`）是同一个目录时，该文件不会被第二次当作项目配置层读取。

语言优先级为默认值 → 用户配置 → 项目配置 → `--language`／运行时配置。`/language` 保存用户偏好并立即应用，保存失败则保留当前语言。未定义 `VIRGO_LANGUAGE` 环境变量。别名与活动会话行为见[本地化](../development/localization.md)。

最小项目配置示例：

```json
{
  "language": "zh-CN",
  "effort": "medium",
  "shell": {"executable": "auto"},
  "permissions": {
    "deny": ["bash(git push*)"]
  }
}
```

可选的模型和扩展文件见[示例](../../../examples/README.zh-CN.md)。凭据应保存在登录存储或环境变量中，不应写入受版本管理的配置。

## 常用配置项

| 键 | 默认值／含义 |
| --- | --- |
| `profile` | `coding`；`minimal` 不选择官方能力 |
| `plugins.<id>.enabled` | 显式布尔值覆盖预设 |
| `budget.maxModelCalls` | `null`；可选的父子运行时共享模型请求正整数上限 |
| `extensions.trustedProjects` | 仅用户配置；获准运行项目 Python 的规范化绝对路径 |
| `language` | `en`；内置语言为 `en` 和 `zh-CN` |
| `model` | `anthropic/claude-opus-5-5`；格式为 `provider/model-id` |
| `effort` | `medium`；界面支持 `low`、`medium`、`high`、`xhigh`、`max` |
| `maxTokens` | 未设置，使用适配器／模型默认值 |
| `shell.executable` | `auto`；受支持的解释器名称或可执行文件路径 |
| `permissions.preset` | `workspace-edit`；选择用户所有的权限预设 |
| `permissions.presets` | 按稳定 ID 保存可编辑名称与三个开关 |
| `sandbox.mode` | 可选的旧禁网兼容配置 |
| `plan` | `false`；`true` 启动可选规划任务 |
| `permissions.approval` | `ask`；`never` 拒绝需要新增批准的操作 |
| `permissions.allow`、`permissions.deny` | 工具规则列表 |
| `compaction.enabled` | `true` |
| `compaction.triggerRatio` | 模型上下文窗口的 `0.8` |
| `compaction.triggerTokens` | 未设置；提供时优先于比例阈值 |
| `compaction.keepRecentTokens` | `20000`，用于通用摘要／Anthropic 路径保留近期上下文 |
| `retry.maxRetries` | `3` |
| `retry.maxWaitSeconds` | 每次逻辑模型调用累计重试等待 `120` 秒 |
| `retry.fallbackModel` | 未设置；可指定过载时的回退模型 |
| `askUser.unattendedTimeout` | 无人值守运行中的提问等待 `300` 秒 |
| `subagents.maxTurns` | `50` |


网页配置见[联网搜索与读取](../guides/web.md)，解释器的具体优先级与平台语义见[命令解释器](../guides/shells.md)。

## 预设与插件

默认 `coding` 启用 `skills`、`todo`、`ask_user`、`diagnostic_get`、`subagent`、`background`、`web`、`code_review`。MCP 仅在存在服务器配置时启用。`minimal` 默认无官方能力；显式 `plugins.<id>.enabled` 可覆盖任一预设。未知插件名和非法值会报告配置错误，不会静默启用能力。

```json
{
  "profile": "minimal",
  "plugins": {"skills": {"enabled": true}, "todo": {"enabled": true}},
  "budget": {"maxModelCalls": 40}
}
```

示例仅选择 Skills 和 Todo 两个官方插件。权限、会话和诊断仍属于 Core。Python 扩展是单独的可信代码路径；使用 `--no-extensions` 禁用。项目不能自行授予信任；应使用 `--trust-project-extensions`，或将绝对目录加入用户 `extensions.trustedProjects`。

旧 `features` 按配置层转换并提示；同层 `plugins` 优先，再应用通常的用户／项目／运行时顺序。`features.permission`、`plugins.auto_review` 和旧权限模式已退休，会报告迁移错误。退役产品不再运行，原文件保留。别名、数据位置和兼容期限见[迁移](../guides/migrating-to-core-plugins.md)。

## 模型与凭据

Provider 配置可以声明 `api`、`baseUrl`、`apiKeyEnv` 和模型元数据：

```json
{
  "providers": {
    "local": {
      "api": "openai",
      "baseUrl": "http://localhost:11434/v1",
      "models": {
        "your-installed-model": {"contextWindow": 32768}
      }
    }
  }
}
```

用 `/model local/your-installed-model` 选择该模型。模型 ID 和上下文窗口需替换为你的服务实际支持的值。`api: "openai"` 使用 Chat Completions，`openai-responses` 使用 Responses，`anthropic` 使用 Messages；账号预设自行配置相应适配器与认证元数据。

启动时，已连接的厂商在后台刷新模型目录，最多四个并发；`/model` 读取缓存快照。显式的 `providers.<id>.models` 字段优先于服务端元数据，之后依次为内置资料和协议兜底值。添加元数据并不等于向远程目录增加模型。Google Code Assist 目前通过直接指定模型使用，没有适配实时目录。刷新失败保留旧快照。

目录缓存按 provider、协议和地址隔离，不保存密钥。Codex 目录的客户端版本从本机、缓存和发布元数据选择，也可用 `providers.openai-codex.clientVersion` 固定。`providers.<id>.apiVersion` 可以固定 Anthropic 协议版本。这些值控制接口兼容性，与 Virgo 应用版本不同。

辅助请求自动选择同一连接的小模型：官方 Anthropic 连接使用 `claude-haiku-5-5`，官方 OpenAI/Codex 连接使用 `gpt-6-luna`，其余使用主模型。推理强度优先 `low`，其次为受支持的默认值，否则省略控制参数。旧 `smallModel` 字段不再参与选择。

## Plan 任务

Plan 和 Code Review 是可选的规划与审查任务。`/plan <任务>` 开始规划，`/plan off`
退出规划但不实施。`/review` 使用正常工具与固定审查证据启动独立审查。两者都不改变权限。
提示词要求输出计划或报告；需要强制保护文件时，另选 Read-Only。
`--plan` 选择初始 Plan 指令，`--format text|json` 控制输出格式。
见[规划](../guides/planning.md)和[代码审查](../guides/code-review.md)。

## 权限

`--permission <id>` 和 `/permissions <id>` 选择权限预设。Shift+Tab 打开 TUI 选择器，可以修改
名称和三个开关，或新建预设。使用只影响当前对话；保存将预设写入用户设置。
任务开始时捕获权限；运行中修改显示为下次生效，不声称撤销已启动进程的权限。
子 Agent 和后台任务保留来源工作区及权限。

| 默认 ID／名称 | 只读 | 仅工作区 | 联网 |
| --- | --- | --- | --- |
| `read-only` / Read-Only | true | true | true |
| `workspace-edit` / Workspace-Edit | false | true | true |
| `no-limit` / No-Limit | false | false | true |

默认 Workspace-Edit。名称只用于显示，实际权限由开关决定。用户设置示例：

```json
{
  "permissions": {
    "preset": "workspace-edit",
    "presets": {
      "offline-inspection": {
        "name": "离线检查",
        "readOnly": true,
        "workspaceOnly": true,
        "networkAllowed": false
      },
      "workspace-edit": {"name": "日常开发"}
    },
    "approval": "ask",
    "deny": ["bash(git push*)"]
  }
}
```

稳定 ID 使用 1–64 个字母、数字、下划线或连字符。`ask`、`never`、`revoke`、`show`、`status`、`sandbox`、`list`、`save` 由 `/permissions` 保留。自定义预设必须提供三个布尔值；
内置预设可只覆盖部分字段。名称使用 1–100 个可打印字符。
逐行客户端使用 `/permissions save <id> <JSON>` 编辑，`/permissions list` 查看实际值，`/permissions show`
分别显示有效权限和后端发现状态。后端不支持所选组合时拒绝执行。

只读保护受管宿主文件数据，私有临时目录和宿主记录仍可写；它不保证远程系统没有副作用。
工作区范围覆盖受管文件读写，并有明确的系统／运行时例外。POSIX 后端和 Core 文件适配器
保护 Virgo 私有授权存储。Windows 使用对象 ACL，存在系统 LPAC 读取授权，不是精确路径命名空间。
网络开关覆盖受管 Shell、Web／原生搜索及远程 MCP 调用，不关闭模型传输或登录。
可信 Python 扩展不在 OS 沙箱中。MCP 服务不能执行本地文件边界，在受限文件模式下需申请
单次、不限文件范围的明确例外。详见[沙箱](../guides/sandbox.md)。

权限范围内的受管操作直接执行，不再根据 Shell 字符串分类进行普通审批。显式 deny 始终生效。
`request_permissions(tool, arguments, permissions, reason)` 申请一个准确操作；批准后模型必须
用相同参数调用该工具。它不会执行或重放命令、修改 Mode、给其他任务授权，历史也不恢复授权。
批准绑定来源 Runtime／运行、参数、cwd、解析路径、工具实例、Shell 环境及策略版本。
变化会使批准失效。已经拒绝的同一请求不会重复询问，直到撤销决定或开始新的运行。

`permissions.approval=ask` 允许交互申请，`never` 拒绝新增申请，但不拒绝已在权限范围内的操作。
`/permissions revoke` 清除记住的批准／拒绝以及尚未消费的单次例外。可信扩展策略仍可提出额外
审批。用户 allow 规则可用于这些策略，但不能突破执行边界或显式 deny。Shell allow 必须精确
匹配完整命令，或授权整个工具；deny 和路径规则支持 glob。

只有用户设置及显式启动覆盖可以选择／编辑权限预设。项目 `permissions` 只接受额外 deny，
不能清空用户拒绝。显式旧 `sandbox.mode=read-only|workspace-write`／`--sandbox` 保留禁网行为；
`disabled` 映射到 No-Limit。删除旧设置后使用新的默认联网预设。不要同时使用 `--permission` 和
`--sandbox`。`sandbox.network`、模型权限审批和旧审批模式别名仍不支持。

## 上下文与重试

所有 provider 使用配置的压缩阈值。受支持的官方 OpenAI/Anthropic 连接使用原生压缩，其他连接使用结构化续接摘要，完整会话历史仍然保留。原生上下文与兼容的模型、系统提示和工具配置绑定，必要时在配置变化后从完整历史重建。`keepRecentTokens` 仅用于保留近期片段的压缩路径，不会再次裁剪 OpenAI 返回的完整窗口。

重试采用带抖动的指数退避，并将有效 `Retry-After` 作为最短等待时间。累计等待预算不包含请求执行时间；下一次等待超过剩余预算时停止重试，不会缩短服务端要求的等待时间。恢复边界见[故障排查](../guides/troubleshooting.md)。

## 环境变量与存储位置

| 名称或路径 | 用途 |
| --- | --- |
| `VIRGO_HOME` | 覆盖用户数据目录，默认 `~/.virgo` |
| `VIRGO_MODEL` | 覆盖配置模型 |
| `VIRGO_SHELL` | 覆盖配置的命令解释器 |
| `NO_COLOR` | 关闭逐行终端颜色 |
| `VIRGO=1` | 工具子进程获得的标记 |
| `<用户目录>/auth.json` | 保存的凭据 |
| `<用户目录>/sessions/` | 各工作空间的会话历史 |
| `<用户目录>/model-metadata/`、`<用户目录>/cache/` | 连接与模型元数据缓存 |
| `<用户目录>/logs/` | 诊断日志，`--no-session` 下也使用 |
| `<项目>/.virgo/outputs/` | 完整工具日志和网页正文 |

历史 Schedule／Workflow／Team／Tasks／Memory／Goal 文件原样保留且不自动执行；位置与人工导出方法见[迁移](../guides/migrating-to-core-plugins.md#只读检查历史数据)。

项目指令读取用户目录的 `AGENTS.md` 和祖先目录中的 `AGENTS.md` 或 `CLAUDE.md`，同层优先 `AGENTS.md`。用户／项目 `.virgo/SYSTEM.md` 可替换身份段落，`APPEND_SYSTEM.md` 可追加指令。技能、模板、Agent、扩展和 MCP 的资源位置见[扩展文档](../development/extensions.md)。
