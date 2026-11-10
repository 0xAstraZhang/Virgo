# Virgo

Virgo 是一个轻量、可扩展的 Coding Agent。

- **架构参考** [Pi](https://github.com/earendil-works/pi)，分三层：统一 LLM 层 → 无 I/O 的 agent 循环 → harness（工具、会话、压缩、扩展）。
- **功能**：支持权限、钩子、TodoWrite、子 Agent、技能、上下文压缩、记忆、任务图、后台任务、定时任务、Agent 团队、MCP、Workflow、Goal loop 等。

```
virgo.ai                          统一流式模型、账号认证 + Anthropic / OpenAI / Google / faux 适配器
virgo.agent                       事件驱动的工具调用循环（steer / follow-up 队列、并行工具、abort）
virgo.harness                     会话树、system prompt、扩展总线、命令、压缩、集成运行时
virgo.tools                       read / write / edit / bash / glob / grep
virgo.features                    内置扩展：权限、todo、子 agent、技能、记忆、任务、后台、定时、团队、MCP、workflow、goal
virgo.cli                         全屏 TUI / plain REPL、-p 打印模式、--mode json 事件流
```

## 快速开始

项目用 [uv](https://docs.astral.sh/uv/) 管理，需要 Python ≥ 3.11。

先安装依赖（已包含 OpenAI 兼容支持）：

```bash
uv sync
```

启动后输入 `/login`，选择账号登录或 API 接入。也可以继续使用环境变量，例如 Claude 的 API 密钥：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

在当前目录启动全屏 TUI：

```bash
uv run virgo
```

也可以通过 `uv run python -m virgo` 启动。版本查询使用 `uv run virgo --version`。

交互终端默认使用全屏界面：上方滚动对话，底部固定输入框和状态栏。顶部显示紫色 `V_` 品牌标识，下方依次显示工作目录和快捷键提示。用户消息用 `>` 标记，浅灰底黑字；模型回复不显示角色名称，其余正文和背景跟随终端配色。退出后恢复原来的终端画面。

需要原来的逐行界面时使用 `uv run virgo --ui plain`。输入或输出不是 TTY、`TERM` 为空或不受支持时，默认自动使用 plain；`--ui fullscreen` 则要求可用的交互终端。

单次执行，只输出最终答案：

```bash
uv run virgo -p "总结 README.md"
```

以 JSONL 事件流输出（便于其他程序集成）：

```bash
uv run virgo -p --mode json "列出所有 TODO 注释"
```

不调用任何模型的离线演示：

```bash
uv run virgo -p "find python files" --model faux/demo
```

去掉 `-p` 即可在全屏界面体验离线演示：`uv run virgo --model faux/demo`。

全局安装为 `virgo` 命令：

```bash
uv tool install .
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `--ui auto\|fullscreen\|plain` | 默认 `auto`；可用交互终端进入全屏，否则使用 plain；不影响 `-p` |
| `-m, --model provider/id` | 初始默认 `anthropic/claude-opus-5-5`；登录后保存所选模型，也可以是 `openai/gpt-5`、`deepseek/deepseek-flash` 等 |
| `--effort low…max` | 推理强度，默认 `medium` |
| `--permission-mode` | `default`（界面显示 `ask before changes`，修改前询问）/ `accept-edits` / `plan`（只读）/ `yolo` |
| `-c` / `-r [FILE]` | 继续最近的会话 / 选择一个会话恢复 |
| `--no-session` | 不保存会话 |
| `--no-extensions` | 不加载用户和项目扩展 |
| `-C DIR` | 工作目录 |

### 全屏交互

- agent 工作时输入的文字会作为 **steering 消息**，在当前这批工具调用结束后、下一次模型调用前插入。
- 只读命令可以在运行中使用；其他命令等待当前运行结束，排队内容显示在输入框上方。
- 后台任务完成、定时任务到点、队友发来消息时，空闲的 agent 会被自动唤醒。
- 工具结果默认显示三行预览，思考默认收起。可以展开单个工具，或用快捷键统一切换详情。
- 模型回复的思考折叠标题用动态星形图标显示思考、回复、工具执行和重试状态，并实时显示本次运行用时；思考时同时显示推理强度，点击三角可展开思考过程。结束后在当前对话中显示总耗时，中断和失败会单独标明。
- 向上滚动时保持当前位置；返回末尾后继续跟随新消息。点击输入框上方的 `↓ Latest message` 可跳到底部，此功能没有快捷键。对话和命令列表隐藏滚动条，仍可用滚轮与键盘滚动；命令介绍换行时保持介绍列对齐。状态栏左侧显示累计 token、估算上下文用量、权限和运行状态，右侧显示模型调用名和推理强度，不显示厂商前缀，不估算费用。
- 鼠标拖选正文或输入框文字后，松开即自动复制，并短暂显示 `Copied`。macOS 本地直接写入系统剪贴板；其他终端使用 OSC52，需终端支持。密码输入框不自动复制。
- `/model` 打开模型选择面板，↑/↓ 选择模型，←/→ 调整思考强度；`/effort` 打开 low → max 档位面板，用 ←/→ 调整。两者均支持 Enter 保存为用户默认值、`s` 仅用于当前会话、Esc 取消。保存只修改对应设置；项目、环境变量和启动参数仍按原有配置优先级生效。带参数的 `/model provider/id` 和 `/effort high` 继续直接修改当前会话。
- 权限请求在弹层中选择允许一次、拒绝或本会话允许。`-r` 无参数时使用全屏会话选择器，恢复后可以查看压缩前的历史。

| 按键 | 作用 |
|---|---|
| Enter | 发送输入；运行中发送 steering 消息 |
| Shift+Enter | 换行；macOS 自带 Terminal 保留 Ctrl+J，见下方说明 |
| Tab | 补全 `/` 命令和 prompt 模板 |
| ↑ / ↓ | 命令列表打开时切换高亮项（默认选中第一项）；否则在输入中移动，到首尾时切换输入历史；Ctrl+P / Ctrl+N 可直接切换历史 |
| Ctrl+O / Ctrl+T | 展开或收起工具详情 / 思考 |
| PageUp / PageDown | 滚动对话 |
| Ctrl+Home | 跳到对话开头 |
| Esc | 先关闭补全或弹层，否则中止运行；审批弹层中表示拒绝 |
| Ctrl+C | 运行时中止；空闲时清空输入；空输入下两秒内连按两次退出 |
| Ctrl+D | 输入为空时退出 |

Shift+Enter 需要终端发送可区分的修饰键序列。macOS 自带 Terminal 会将 Shift+Enter 和 Enter 都发为回车，Virgo 无法区分；此时保留 Ctrl+J 换行，底部提示也显示 Ctrl+J。不更改终端设置。

plain 模式保留逐行输入和原有行为：`Ctrl-C` 中止运行，空闲时两秒内连按两次退出。

退出时会先停止后台任务并清理会话。如果关闭钩子仍在等待，可以按 `Ctrl+C` 返回终端。

### 命令

| 命令 | 作用 |
|---|---|
| `/help` | 列出命令与 prompt 模板 |
| `/login [auth\|api] [provider]` | 账号登录或配置 API；无参数进入选择器 |
| `/logout [provider\|all]` | 选择并删除已保存的登录，或退出全部账号 |
| `/model [p/id]` `/effort [lvl]` | 查看或切换模型、推理强度 |
| `/permissions [mode]` | 查看规则 / 切换权限模式 |
| `/compact [focus]` | 立即压缩上下文 |
| `/new` `/resume [n]` `/session` | 会话管理 |
| `/todos` `/tasks` `/jobs` `/schedules` `/team` | 查看各功能状态 |
| `/skills` `/skill:name args` | 列出技能 / 强制使用某个技能 |
| `/memory [tidy]` | 列出记忆 / 用小模型整理去重 |
| `/mcp [reconnect]` | MCP 服务器状态 |
| `/workflow [resume id]` | 列出 workflow / 续跑 |
| `/goal <条件>` `/goal` `/goal clear` | 目标循环 |
| `/exit` | 退出 |

### 登录模型服务

`/login` 在全屏界面复用底部命令区域，选择、文本输入和密码输入都在原界面完成；plain 界面使用编号菜单。先选择 **Auth — 登录账号** 或 **API — API 接入**：

| 方式 | 预设 | 接入方式 |
|---|---|---|
| Auth | OpenAI | ChatGPT 账号，浏览器回调、粘贴回调地址或设备码；使用 Codex 接口 |
| Auth | Anthropic | Claude 账号，浏览器回调或复制授权码 |
| Auth | xAI | 设备码登录 |
| Auth | Google | Google 账号，浏览器回调或复制授权码；使用 Code Assist |
| Auth | Kimi | Kimi Code 账号，设备码登录 |
| API | DeepSeek、GLM、Kimi | 预填服务地址和模型，输入自己的 API key |
| API | 自定义 | 输入服务商 ID、接口协议、Base URL、模型 ID 和 API key；支持 OpenAI 兼容和 Anthropic Messages |

也可以直接指定入口：

```text
/login auth openai
/login auth anthropic
/login auth xai
/login auth google
/login auth kimi
/login api deepseek
/login api glm
/login api kimi
/login api custom
/logout
/logout deepseek
/logout all
```

API 接入时可以修改预填的地址和模型。GLM 默认使用普通 API 地址，Coding Plan 用户可改为 `https://open.bigmodel.cn/api/coding/paas/v4`；Kimi API 默认使用 `https://api.moonshot.cn/v1`，国际站可改为 `.ai` 地址。Kimi 账号登录保存在 `kimi-coding` 下，API 接入保存在 `kimi` 下，两者可分别退出。

登录成功后立即切换模型，并把模型及接口配置写入用户级 `settings.json`；项目配置、`VIRGO_MODEL` 和启动参数仍按原有优先级覆盖。Google 登录如需用户项目，会提示输入 Google Cloud project ID，也会读取 `GOOGLE_CLOUD_PROJECT` 或 `GOOGLE_CLOUD_PROJECT_ID`。

密钥和 OAuth token 单独保存到 `~/.virgo/auth.json`（受 `VIRGO_HOME` 控制），文件权限为 `0600`。密钥输入不会显示或进入聊天、会话文件和输入历史。每次模型请求读取当前凭据，OAuth 临近过期时刷新；已保存凭据优先于环境变量。凭据绑定登录时选定的接口，修改协议或地址后需重新登录。`/logout` 删除所选本地凭据，环境变量中的密钥仍然有效；自定义接入使用独立的 `VIRGO_<PROVIDER_ID>_API_KEY`（ID 转为大写，连字符改为下划线）。全屏可按 Esc 取消当前步骤；两种界面都可用 Ctrl+C 中止登录。

实现参考 [Pi 1.5.0](https://github.com/earendil-works/pi) 的 AuthStorage、OAuth 和模型适配器；Google 账号协议参考 [Pi v0.56.1](https://github.com/badlogic/pi-mono/blob/v0.56.1/packages/ai/src/utils/oauth/google-gemini-cli.ts) 及 [Gemini CLI](https://github.com/google-gemini/gemini-cli)。第三方许可见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。

## 配置

配置分三层，后者覆盖前者：内置默认值 → `~/.virgo/settings.json` → `<项目>/.virgo/settings.json`（深合并）。完整示例见 [`examples/.virgo/`](examples/.virgo)。

`VIRGO_HOME` 可指定用户配置目录，`VIRGO_MODEL` 可覆盖配置中的模型，`VIRGO_SHELL` 可指定工具使用的 shell。设置 `VIRGO_DEBUG` 会显示扩展事件异常；工具启动的子进程带有 `VIRGO=1` 标记。

```json
{
  "model": "anthropic/claude-opus-5-5",
  "smallModel": "anthropic/claude-haiku-5-5",
  "effort": "medium",
  "permissions": {"mode": "default", "allow": ["bash(uv run pytest*)", "edit(src/**)"], "deny": ["bash(git push*)"]},
  "providers": {
    "deepseek": {"api": "openai", "baseUrl": "https://api.deepseek.com", "apiKeyEnv": "DEEPSEEK_API_KEY",
                 "models": {"deepseek-flash": {"contextWindow": 1000000}}}
  },
  "hooks": {"PreToolUse": [{"matcher": "bash", "command": "./scripts/guard.sh"}]},
  "compaction": {"reserveTokens": 16384, "keepRecentTokens": 20000, "triggerTokens": null},
  "retry": {"maxRetries": 3, "fallbackModel": null},
  "features": {"team": true, "schedule": true}
}
```

- **模型提供方**：Anthropic、OpenAI 兼容和 xAI Responses 使用官方 SDK；OpenAI 账号与 Google 账号使用对应的流式接口。也可以在 `providers` 里声明自定义端点。`smallModel` 用于压缩摘要、goal 评审和记忆整理；切换到其他提供方时，未另配的小模型跟随当前模型。
- **权限规则**：写法是 `tool(pattern)`，pattern 为 fnmatch 风格，匹配命令或路径。例如 `bash(npm test*)`、`edit(src/**)`、`read(~/.ssh/*)`、`mcp__github__*`。
- **命令钩子**：支持 `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop` 四种。钩子从 stdin 收到 JSON；退出码 2 表示拦截（或要求继续），stderr 的内容作为反馈。
- **功能开关**：每个内置功能都可以用 `features.<name>: false` 关闭（例如 `team`、`mcp`、`memory`）。
- **上下文文件**：`~/.virgo/AGENTS.md`，加上从根目录到 cwd 每一层的 `AGENTS.md` 或 `CLAUDE.md`。`SYSTEM.md` 替换身份段落，`APPEND_SYSTEM.md` 追加到末尾。

## 扩展

扩展放在 `~/.virgo/extensions/*.py` 或 `.virgo/extensions/*.py`。内置功能和用户扩展使用同一套 API：

```python
from virgo.agent import AgentTool, Block

def setup(api):
    api.on("tool_call", lambda ev, s: Block("禁止写 .env") if str(ev.args.get("path", "")).endswith(".env") else None)
    api.register_tool(AgentTool("now", "当前时间", {"type": "object"},
                                lambda args, ctx: __import__("time").ctime(), read_only=True))
    api.register_command("hi", lambda session, args: api.ui.print("hello"), "打个招呼")
    api.add_system_section("team-rules", lambda role: "<rules>提交前先跑测试</rules>")
    # api.notify(text) 可以唤醒空闲的 agent；api.append_entry(kind, data) 把状态写入会话
```

可订阅的事件有：`session_start`、`session_shutdown`、`input`、`before_agent_start`、`agent_event`、`context`、`tool_call`、`tool_result`、`turn_end`、`agent_stop`、`agent_end`、`before_compact`、`compact`、`pending_work`。详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

其他可扩展资源都是放进对应目录的文件：

- **技能**：`skills/<name>/SKILL.md`。frontmatter 写 `name` 和 `description`；system prompt 里只列出名称、描述和路径，用到时模型再去读全文。
- **子 agent 类型**：`agents/<name>.md`。frontmatter 可写 `tools`、`model`、`permission`，正文是 system prompt。
- **Prompt 模板**：`prompts/<name>.md` 会变成 `/name` 命令，支持 `$1`、`$@`、`${1:-默认值}`。
- **Workflow**：`workflows/<name>.py`，导出 `META` 和 `run(ctx, args)`。可用原语有 `ctx.agent(prompt, schema=, tools=)`、`ctx.parallel`、`ctx.map`、`ctx.phase`、`ctx.log`。
- **MCP**：`mcp.json`，格式为 `{"mcpServers": {name: {command, args, env}}}`。

## 功能与实现

| 功能 | Virgo 实现 | 设计要点 |
|---|---|---|
| Agent 循环 | `agent/loop.py` | 双层循环：工具调用 / steering 驱动内层，follow-up 和停止钩子驱动外层；事件化，无 I/O |
| 工具 | `tools/`、`AgentTool` | 工具注册即分发；`content` 给模型，`details` 给 UI 和状态；默认并行执行，同一文件的写操作串行；输出统一截断并落盘 |
| 权限 | `features/permission.py` | 四种模式；判定顺序：硬拒绝 → deny 规则 → plan 模式 → allow 规则 → 默认策略；bash 先按 `&& ; \|` 拆段再判断是否只读；无人值守时"询问"视为拒绝 |
| 钩子 | `harness/extensions.py` | 带优先级的事件总线：`tool_call` 可拦截或改参，`tool_result` 可改结果，`agent_stop` 可要求继续；另有 shell 命令钩子 |
| TodoWrite | `features/todo.py` | 整表替换、校验失败不改状态；状态存在工具结果 `details` 里，可从会话重建；只在 agent 准备结束但仍有未完成项时提醒一次 |
| 子 Agent | `features/subagent.py` | 全新上下文，只返回最终答案；不能递归；`general` / `explore` 两种类型，可在 `agents/*.md` 自定义；可并行、可后台 |
| 技能 | `features/skills.py` | 渐进披露：prompt 只列目录，模型用 `read` 加载正文；`/skill:` 强制使用 |
| 上下文压缩 | `harness/compaction.py`、`tools/truncate.py` | 单条输出预算 + 自动结构化摘要（增量更新）+ 超限时反应式压缩 + `/compact`；切点不拆开 tool 调用与结果，保留轮次剥离 thinking |
| 记忆 | `features/memory.py` | 用户级和项目级两层，每条记忆一个 Markdown 文件，各配一个 `MEMORY.md` 索引；会话开始时把索引放进 prompt；`memory` 工具主动保存并去重；`/memory tidy` 整理，失败回滚 |
| 任务系统 | `features/tasks.py` | 持久化依赖图 + 文件锁；认领时校验依赖；做环检测；完成时报告新解锁的任务；与队友共享 |
| 后台任务 | `features/background.py` | bash 和子 agent 共用一个 JobManager；完成后通知（agent 运行中则插入，空闲则唤醒） |
| 定时任务 | `features/schedule.py` | 5 字段 cron 加 `at`（ISO 时间或 `+10m`）；到点作为 follow-up 送达；可持久化 |
| Agent 团队 | `features/team.py` | 队友各占一个线程，有独立会话和进程内邮箱；空闲时先收邮件再自动认领任务；停止和计划审批都是带 request_id 的协议；计划审批复用权限的 plan 模式；可为队友建独立 git worktree |
| MCP | `features/mcp.py` | 真正的 stdio JSON-RPC 客户端（initialize → tools/list → tools/call）；工具命名 `mcp__server__tool`；默认需要授权 |
| 集成 harness | `harness/session.py` | 所有功能都以扩展身份接入同一个循环；负责重试、回退模型、反应式压缩和统一唤醒 |
| Workflow | `features/workflow.py` | 编排由脚本固定，模型只负责调用；结构化输出带校验和重试；journal 按调用内容哈希，续跑时已完成的调用直接命中 |
| Goal loop | `features/goal.py` | 每次准备停止时由独立的无工具评审判断：达成 / 不可能 / 未满足则继续；有上限；后台任务未完成时暂缓评审 |

## 设计取舍

- **发送给 API 的历史只追加。** 
  - system prompt 和工具集合在会话开始时冻结，只在压缩后重建；
  - 通知、提醒、队友消息都以追加消息的形式进入；
  - 不改写、不删除旧轮次，所以**不做 micro-compaction**，也不做中段 snip；
  - 完整历史始终保存在会话文件中。
  - 有测试逐字节检查相邻两次请求的前缀是否一致。
- **少即是多。** 能复用的机制尽量复用：计划审批复用权限模式；后台任务同时承载 bash 和子 agent；记忆和技能共用"索引 + 按需 `read`"的渐进披露方式，因此每轮不会产生额外的模型调用。
- **失败即数据。** provider 的流和工具都不会把异常抛进循环，错误会变成消息交给模型处理。
- **Anthropic 请求形态**：
  - adaptive thinking，用 `output_config.effort` 控制深度；
  - 顶层自动 prompt caching；
  - 工具开启 `eager_input_streaming`，参数在本地校验；
  - 默认开启服务端拒答回退（`fallbacks: "default"`），可以在 `anthropic.fallbacks` 关闭；
  - 带上 thinking `drop_block` 作为安全网。

## 开发

运行全部测试（基于 faux provider、本地假 HTTP 服务器、OAuth 模拟响应和 PTY，不需要真实账号或密钥）：

```bash
uv run pytest -q
```

静态检查：

```bash
uv run ruff check
```

打包：

```bash
uv build
```

升级版本号：

```bash
uv version --bump patch
```

## License

MIT
