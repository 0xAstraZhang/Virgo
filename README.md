# Virgo

Virgo 是一个轻量、可扩展的 Coding Agent。

- **架构参考** [Pi](https://github.com/earendil-works/pi)，分三层：统一 LLM 层 → 无 I/O 的 agent 循环 → harness（工具、会话、压缩、扩展）。
- **功能**：支持权限、钩子、TodoWrite、子 Agent、技能、上下文压缩、记忆、任务图、后台任务、定时任务、Agent 团队、MCP、Workflow、Goal loop 等。

```
virgo.ai                          统一的流式消息模型 + Anthropic / OpenAI 兼容 / faux 适配器
virgo.agent                       事件驱动的工具调用循环（steer / follow-up 队列、并行工具、abort）
virgo.harness                     会话树、system prompt、扩展总线、命令、压缩、集成运行时
virgo.tools                       read / write / edit / bash / glob / grep
virgo.features                    内置扩展：权限、todo、子 agent、技能、记忆、任务、后台、定时、团队、MCP、workflow、goal
virgo.cli                         REPL、-p 打印模式、--mode json 事件流
```

## 快速开始

项目用 [uv](https://docs.astral.sh/uv/) 管理，需要 Python ≥ 3.11。

先安装依赖（含 OpenAI 兼容支持）：

```bash
uv sync --all-extras
```

配置 Claude 的 API 密钥：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

在当前目录启动交互式 REPL：

```bash
uv run virgo
```

也可以通过 `uv run python -m virgo` 启动。版本查询使用 `uv run virgo --version`。

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

全局安装为 `virgo` 命令：

```bash
uv tool install .
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-m, --model provider/id` | 默认 `anthropic/claude-opus-5-5`；也可以是 `openai/gpt-5`、`deepseek/deepseek-chat` 等 |
| `--effort low…max` | 推理强度，默认 `medium` |
| `--permission-mode` | `default`（改动前询问）/ `accept-edits` / `plan`（只读）/ `yolo` |
| `-c` / `-r [FILE]` | 继续最近的会话 / 选择一个会话恢复 |
| `--no-session` | 不保存会话 |
| `--no-extensions` | 不加载用户和项目扩展 |
| `-C DIR` | 工作目录 |

### REPL 交互

- agent 工作时输入的文字会作为 **steering 消息**，在当前这批工具调用结束后、下一次模型调用前插入。
- `Ctrl-C` 中止当前运行；空闲时连按两次退出。
- 后台任务完成、定时任务到点、队友发来消息时，空闲的 agent 会被自动唤醒。

| 命令 | 作用 |
|---|---|
| `/help` | 列出命令与 prompt 模板 |
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
                 "models": {"deepseek-chat": {"contextWindow": 128000}}}
  },
  "hooks": {"PreToolUse": [{"matcher": "bash", "command": "./scripts/guard.sh"}]},
  "compaction": {"reserveTokens": 16384, "keepRecentTokens": 20000, "triggerTokens": null},
  "retry": {"maxRetries": 3, "fallbackModel": null},
  "features": {"team": true, "schedule": true}
}
```

- **模型提供方**：Anthropic 走官方 SDK；任何 OpenAI 兼容端点（DeepSeek、Qwen、Ollama、vLLM 等）都可以在 `providers` 里声明。`smallModel` 用于压缩摘要、goal 评审和记忆整理。
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

运行全部测试（全部基于 faux provider 和本地假 HTTP 服务器，不需要网络或密钥）：

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
