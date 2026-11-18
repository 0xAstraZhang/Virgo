# 配置参考

[文档首页](../README.md) · [English](../../en/reference/configuration.md)

## 文件与优先级

Virgo 按以下顺序深合并内置默认值、用户配置和项目配置：

1. [`harness/settings.py`](../../../src/virgo/harness/settings.py) 中的默认值。
2. `~/.virgo/settings.json`，或 `$VIRGO_HOME/settings.json`。
3. `<工作目录>/.virgo/settings.json`。
4. 支持的环境变量和运行时／CLI 覆盖值。

嵌套对象合并，高优先级值覆盖低优先级值。项目配置不能提供执行权限或扩展信任，授权必须来自用户配置或显式启动参数。CLI 指定的模型覆盖 `VIRGO_MODEL`，后者覆盖配置文件。`VIRGO_SHELL` 覆盖用户、项目和运行时指定的解释器。命令保存用户默认值时会保留无关字段；下次启动时，项目或启动参数仍可能优先。当 `<工作目录>/.virgo` 与用户配置目录（`~/.virgo` 或 `$VIRGO_HOME`）是同一个目录时，该文件不会被第二次当作项目配置层读取。

语言优先级为默认值 → 用户配置 → 项目配置 → `--language`／运行时配置。`/language` 保存用户偏好并立即应用，保存失败则保留当前语言。未定义 `VIRGO_LANGUAGE` 环境变量。别名与活动会话行为见[本地化](../development/localization.md)。

最小项目配置示例：

```json
{
  "language": "zh-CN",
  "effort": "medium",
  "shell": {"executable": "auto"}
}
```

可选的模型和扩展文件见[示例](../../../examples/README.zh-CN.md)。凭据应保存在登录存储或环境变量中，不应写入受版本管理的配置。

## 常用配置项

| 键 | 默认值／含义 |
| --- | --- |
| `plugins.<id>.enabled` | 显式布尔值覆盖插件默认值 |
| `budget.maxModelCalls` | `null`；可选的父子运行时共享模型请求正整数上限 |
| `extensions.trustedProjects` | 仅用户配置；获准运行项目 Python 的规范化绝对路径 |
| `language` | `en`；内置语言为 `en` 和 `zh-CN` |
| `model` | `anthropic/claude-opus-5-5`；格式为 `provider/model-id` |
| `effort` | `medium`；界面支持 `low`、`medium`、`high`、`xhigh`、`max` |
| `maxTokens` | 未设置，使用适配器／模型默认值 |
| `shell.executable` | `auto`；受支持的解释器名称或可执行文件路径 |
| `execution.mode` | `no-limit`（默认）、`read-only`、`workspace-edit` |
| `permissions.approval` | `ask`；`never` 拒绝需要新增批准的操作 |
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

## 插件

官方插件 `skills`、`todo`、`ask_user`、`diagnostic_get`、`subagent`、`background` 和 `web` 默认启用。显式 `plugins.<id>.enabled` 覆盖该默认值。未知插件名和非法值会报告配置错误，不会静默启用能力。[MCP 扩展](../../../extensions/README.zh-CN.md)是单独的启用路径，不是插件 ID。

```json
{
  "plugins": {"todo": {"enabled": true}, "web": {"enabled": false}},
  "budget": {"maxModelCalls": 40}
}
```

示例显式保留 Todo 并关闭 Web，其余官方插件保持默认。权限、会话和诊断仍属于 Core。Python 扩展是单独的可信代码路径；使用 `--no-extensions` 禁用。项目不能自行授予信任；应使用 `--trust-project-extensions`，或将绝对目录加入用户 `extensions.trustedProjects`。

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

启动时，已连接的厂商在后台刷新模型目录，最多四个并发。`/login` 成功后还会拉取该厂商的模型目录一次，使 `/model` 立即有可用条目；这次拉取失败只会打印警告。`/model` 读取缓存快照。显式的 `providers.<id>.models` 字段优先于服务端元数据，之后依次为内置资料和协议兜底值。添加元数据并不等于向远程目录增加模型。刷新失败保留旧快照。

目录缓存按 provider、协议和地址隔离，不保存密钥。Codex 目录的客户端版本从本机 npm 包清单（不执行 Codex）、缓存和官方发布元数据选择；没有包清单的独立安装使用缓存或发布来源，也可用 `providers.openai-codex.clientVersion` 固定。`providers.<id>.apiVersion` 可以固定 Anthropic 协议版本。这些值控制接口兼容性，与 Virgo 应用版本不同。

辅助请求自动选择同一连接的小模型：官方 Anthropic 连接使用 `claude-haiku-5-5`，官方 OpenAI/Codex 连接使用 `gpt-6-luna`，其余使用主模型。推理强度优先 `low`，其次为受支持的默认值，否则省略控制参数。旧 `smallModel` 字段不再参与选择。

## 执行与资源权限

使用 `--permission read-only`、`--permission workspace-edit` 或 `--permission no-limit`。新安装默认 No-Limit。兼容参数 `--execution host` 选择 No-Limit。CLI 与 TUI 提供相同的三种模式。

```json
{"execution":{"mode":"workspace-edit"}}
```

`/permissions show` 显示当前任务和下一任务的模式，`/permissions list` 列出模式。使用 `/permissions grants` 查看持久授权，`/permissions revoke ID` 撤销指定授权。项目没有授予执行权限的入口。运行中的子任务与后台操作保持已捕获的上下文。

详见[权限模式](../development/permission-profiles.md)及[原生后端](../guides/sandbox.md)。

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
