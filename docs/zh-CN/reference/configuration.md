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
    "mode": "auto-review",
    "allow": ["bash(uv run pytest*)"],
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
| `sandbox.mode` | `disabled`；可显式选择 `read-only` 或 `workspace-write`，仅用户／启动配置生效 |
| `sandbox.network` | `restricted`；不支持活动网络例外 |
| `workMode` | `default`；`plan` 增加独立的只读权限上限 |
| `permissions.mode` | `auto-review` |
| `permissions.allow`、`permissions.deny` | 工具规则列表 |
| `permissions.readOnlyCommands` | 额外纳入只读执行判断的命令 |
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

默认 `coding` 启用 `skills`、`todo`、`ask_user`、`diagnostic_get`、`subagent`、`background`、`web`、`auto_review`。MCP 仅在存在服务器配置时启用。`minimal` 默认无官方能力；显式 `plugins.<id>.enabled` 可覆盖任一预设。未知插件名和非法值会报告配置错误，不会静默启用能力。

```json
{
  "profile": "minimal",
  "plugins": {"skills": {"enabled": true}, "todo": {"enabled": true}},
  "budget": {"maxModelCalls": 40}
}
```

示例仅选择 Skills 和 Todo 两个官方插件。权限、会话和诊断仍属于 Core。Python 扩展是单独的可信代码路径；使用 `--no-extensions` 禁用。项目不能自行授予信任；应使用 `--trust-project-extensions`，或将绝对目录加入用户 `extensions.trustedProjects`。

旧 `features` 按配置层转换并提示；同层 `plugins` 优先，再应用通常的用户／项目／运行时顺序。`features.permission=false` 只关闭模型审查，不会关闭 Core 权限门。退役产品不再运行，原文件保留。别名、数据位置和兼容期限见[迁移](../guides/migrating-to-core-plugins.md)。

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

`/mode plan` 进入只读规划，`/mode default` 返回 Default，不执行方案；`/mode` 查看当前工作模式。这些 Core 命令在 `--profile minimal --no-extensions` 下也可用，不会请求模型。`/plan` 显示当前 ProposedPlan，`/plan edit` 替换 Markdown，`/plan execute <plan-id>:<revision>` 明确开始执行该版本。完整操作与编辑示例见[先规划，再执行](../guides/planning.md)。

工作模式与审批策略分别保存。`/permissions accept-all` 或 `/permissions auto-review` 只改变审批策略，不会退出独立的 Plan 模式。返回 `default` 时保留已选择的审批策略，不会自行授予 `accept-all`。两个维度可分别配置：

```json
{
  "workMode": "plan",
  "permissions": {"mode": "auto-review"}
}
```

同一套有效策略用于过滤模型可见工具、钩子前预检和执行前复检。Plan 上限跟随来源主 Runtime 及其子运行时，在切换对话后仍然有效；退出 Plan 不会移除子运行时自身的只读限制。进入 Plan 会约束尚未开始的调用；对话繁忙时拒绝退出 Plan 或执行计划，已经运行的进程保留启动时策略。权限审计保留有效 `mode`，并分别记录 `work_mode` 和 `permission_mode`。

旧 `permissions.mode=plan`、`--permission-mode plan` 和直接使用 `session.permission_mode` 的 API 仍被接受。`/permissions plan` 现在进入独立工作模式，不替换已选择的审批策略。没有显式传入构造参数 `work_mode` 时，旧 Plan 启动也会为历史恢复保存 Plan 限制，即使尚未产生提案。模式或审批命令移除旧的全局限制前，会先为其他所有来源对话及其后代保留 Plan；状态保存失败时继续保留全局限制。`/mode default` 只解除所选对话的 Plan；若此前没有另外显式选择审批策略，旧 Plan 启动会恢复为 `auto-review`。集成代码应使用 `session.services["permissions"].set_work_mode(...)` 切换独立模式；旧的直接赋值 API 保留兼容行为。

工作模式和当前 ProposedPlan 是通过 SessionStore 保存的对话状态。新对话使用配置的 `workMode`；切回已保留对话时使用其原 Runtime 和模式。打开保存的历史会恢复已保存的模式，但配置 `workMode: "plan"` 会保留 Plan 上限。显式 `--work-mode plan|default` 或 `AgentSession(work_mode="plan" | "default")` 覆盖初始对话的独立模式，不改变后续新对话的默认值。模式命令不保存用户偏好。

恢复计划会清除本次会话的确认，不开始或排队执行。审批策略和 Sandbox 来自当前配置，不从历史计划元数据恢复。修改 Markdown 会递增版本并使旧确认失效；执行需要准确的当前 token，且对话必须空闲。计划确认不等于对后续工具调用的预先批准，也不解除 Sandbox。CLI 的 `--mode text|json` 仍控制输出格式，与 `--work-mode` 独立。全屏界面中，Shift+Tab 只切换 WorkMode，Ctrl+G 打开计划编辑窗口。选择 WorkMode 本身不启用操作系统沙箱，也不开始代码审查任务。

## 权限

| 模式 | 行为 |
| --- | --- |
| `auto-review` | 放行能确认工作区边界的操作；用小模型审查目录外、联网和边界不明的操作 |
| `accept-all` | 默认工作模式下跳过审批及 allow/deny 规则；不能覆盖独立 Plan 上限 |
| `plan` | 旧只读入口；建议使用独立的 `/mode plan` 或 `workMode` 配置 |

`auto_review` 插件提供模型审查；禁用后 Core 仍执行规则，不确定操作需要人工确认，非交互会话无法批准。自动审查分别判断用户授权和风险，返回 `allow`、`ask` 或 `deny`。证据不足或审查失败时进入现有人工确认；非交互运行无法批准该问题。明确拒绝会阻止操作。参数变化后，在工具真正执行前还会再次检查。这些是应用层检查，与需要显式启用的[原生命令沙箱](../guides/sandbox.md)分离。`accept-all` 不会绕过已启用的沙箱。

规则采用 `tool(pattern)`，其中 pattern 使用 fnmatch 风格，也可以只写工具名模式。文件工具匹配路径，命令工具匹配命令，网页工具匹配查询或 URL。例如 `edit(src/**)`、`read(~/.ssh/*)`、`bash(npm test*)` 和 `mcp__server__*`。PowerShell 使用独立的 `powershell(...)` 规则。顺序见 [`harness/permission_policy.py`](../../../src/virgo/harness/permission_policy.py)，边界判断见 [`permission_scope.py`](../../../src/virgo/harness/permission_scope.py)。

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
