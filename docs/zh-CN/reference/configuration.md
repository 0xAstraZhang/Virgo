# 配置参考

[文档首页](../README.md) · [English](../../en/reference/configuration.md)

## 文件与优先级

Virgo 按以下顺序深合并内置默认值、用户配置和项目配置：

1. [`harness/settings.py`](../../../src/virgo/harness/settings.py) 中的默认值。
2. `~/.virgo/settings.json`，或 `$VIRGO_HOME/settings.json`。
3. `<工作目录>/.virgo/settings.json`。
4. 支持的环境变量和运行时／CLI 覆盖值。

嵌套对象合并，高优先级值覆盖低优先级值。CLI 指定的模型覆盖 `VIRGO_MODEL`，后者覆盖配置文件。`VIRGO_SHELL` 覆盖用户、项目和运行时指定的解释器。命令保存用户默认值时会保留无关字段；下次启动时，项目或启动参数仍可能优先。

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
| `sandbox.mode` | `workspace-write`；可选择 `read-only` 或显式 `disabled`，仅用户／启动配置生效 |
| `workMode` | `default`；`plan` 增加独立的只读权限上限 |
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

## 工作模式

`/mode plan` 进入规划，`/mode default` 返回普通工作，不会直接执行提案。`--work-mode default|plan` 选择初始对话的工作意图，`--format text|json` 控制输出格式。它们独立于 `--approval ask|never` 和 `--sandbox`。详见[规划变更](../guides/planning.md)。

```json
{
  "workMode": "plan",
  "permissions": {"approval": "ask"},
  "sandbox": {"mode": "workspace-write"}
}
```

此示例属于用户设置。项目可以请求 Plan，但不能用 `workMode=default` 放宽用户配置的 Plan 上限；用户可通过显式启动参数选择其他模式。已有对话保存自己的工作模式，新对话使用配置默认值。切换工作模式要求来源对话及其排队、运行中的后代任务空闲。通知只观察提交后的状态，不能重入模式、计划或运行准入变更。

Plan 与只读子运行时只暴露只读工具，包括受控 `git_read`，不提供任意 Bash/PowerShell。子运行时只读能力独立于工作意图；只读调查或 Code Review 不会因此收到 Plan 指令。子运行时继承来源工作区，只能进一步收紧父级能力和审批上限。

计划在 SessionStore 中保存版本化 Markdown。`/plan execute <id>:<revision>` 将准确版本绑定到一次执行请求；保存或恢复提案不会执行。执行保留审批及 Sandbox 限制；返回 Default 不会把配置为只读的沙箱改成可写。已退休入口见[执行策略迁移](../guides/migrating-execution-policy.md)。

## 权限

| 审批策略 | 行为 |
| --- | --- |
| `ask` | 执行已经获授权的操作；不确定或尚未获授权的操作询问用户 |
| `never` | 执行已经获授权的操作；需要新增批准的操作直接拒绝 |

Core 授权检查始终启用。不会再调用模型从对话历史、工具输出或摘要推断授权。两种策略均保留显式 deny、只读能力和 Sandbox 上限。`/permissions ask` 与 `/permissions never` 选择策略，`/permissions revoke` 清除记住的批准和拒绝；这些命令不改变 WorkMode 或 Sandbox。

工作区文件操作使用确定性路径策略。简单本地 Shell 形式仅在原生沙箱启用时可自动允许；命令分类只是便利判断，不能证明隔离。无隔离宿主 Shell、网络、工作区外和未知操作需要明确规则或人工批准。沙箱失败不授予宿主重试权；没有交互审批者时，未解决的批准请求会拒绝执行。

只有用户设置和显式启动覆盖可授予权限或选择审批策略。项目 `permissions` 只接受 `deny`，并与用户拒绝列表合并。项目 `allow`、`approval`、旧 `mode` 或 `readOnlyCommands` 会报告配置错误；项目空列表不能清空用户 deny。

规则采用 `tool(pattern)` 或单独的工具名称模式。文件路径和 Web 规则使用 fnmatch。Shell allow 规则匹配**完整、准确的命令**，包含通配符的命令授权会报错；只有确实授权整个能力时才使用单独的 `bash` 或 `powershell`。Shell deny 仍可使用宽范围模式。用户设置示例：

```json
{
  "permissions": {
    "approval": "ask",
    "allow": ["bash(uv run --no-sync pytest -q)", "read(/opt/reference/**)"],
    "deny": ["bash(git push*)", "read(~/.ssh/*)"]
  }
}
```

Deny 和能力检查先于授权与询问。参数变化后，执行前再次核验。记住的批准绑定来源任务、准确参数、cwd、可静态解析的路径目标、Shell 环境和显式策略状态。动态 Shell 路径与 PowerShell 脚本仅获得单次批准。策略变化或 `/permissions revoke` 使旧决定失效；新增自然语言消息本身不属于授权系统状态变更。

审计记录保留来源任务、工作模式、只读上限、审批策略、决定和参数摘要。权限模型的风险及证据记录已退休。应用层检查与[原生 Shell 沙箱](../guides/sandbox.md)相互独立；后者不隔离可信 Python 扩展或远程工具。

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
