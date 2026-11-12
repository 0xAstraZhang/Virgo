# Virgo

Virgo 是一个轻量、可扩展的 Coding Agent。Harness 提供工具能力、环境事实和执行保障，由模型判断工作方法与任务进展。

- **架构参考** [Pi](https://github.com/earendil-works/pi)，分三层：统一 LLM 层 → 无 I/O 的 agent 循环 → harness（工具、会话、压缩、扩展）。
- **功能**：支持权限、钩子、TodoWrite、主动提问（ask_user）、子 Agent、技能、上下文压缩、记忆、任务图、后台任务、定时任务、Agent 团队、MCP、Workflow、Goal loop 等。

```
virgo.ai                          统一流式模型、账号认证 + Anthropic / OpenAI / Google / faux 适配器
virgo.agent                       事件驱动的工具调用循环（steer / follow-up 队列、并行工具、abort）
virgo.harness                     会话树、system prompt、扩展总线、命令、压缩、集成运行时
virgo.tools                       read / write / edit / bash 或 powershell / glob / grep / web_search
virgo.features                    内置扩展：权限、todo、ask_user、子 agent、技能、记忆、任务、后台、定时、团队、MCP、workflow、goal
virgo.cli                         全屏 TUI / plain REPL、-p 打印模式、--mode json 事件流
```

## 快速开始

项目用 [uv](https://docs.astral.sh/uv/) 管理，需要 Python ≥ 3.11。

先安装依赖（已包含 OpenAI 兼容支持）：

```bash
uv sync
```

Codex 本地环境配置保存在 `.codex/environments/environment.toml`，应随项目一起纳入 Git。
当前配置保留空的 setup 脚本，依赖仍按上面的命令手动安装。
若创建工作树时报该文件 `ENOENT`，先将配置恢复到所选环境对应的源码目录，
再重试；仅在其他工作树中恢复文件不会修复原目录的缺失路径。

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
| `--permission-mode` | `auto-review`（默认，目录外和联网操作由小模型审批）/ `accept-all`（全部放行）/ `plan`（只读规划） |
| `-c` / `-r [FILE]` | 继续最近的会话 / 选择一个会话恢复 |
| `--no-session` | 不保存会话 |
| `--no-extensions` | 不加载用户和项目扩展 |
| `-C DIR` | 工作目录 |

### 全屏交互

- agent 工作时输入的文字会作为 **steering 消息**，在当前这批工具调用结束后、下一次模型调用前插入。
- 只读命令可以在运行中使用；其他命令等待当前运行结束，排队内容显示在输入框上方。
- 后台任务完成、定时任务到点、队友发来消息时，空闲的 agent 会被自动唤醒。
- 每轮运行的思考、中间消息、工具调用、工具结果和通知按产生顺序放在同一个 **Thinking** 块下；用户提问和最终回复保留在外面。Thinking 和各事件默认收起，展开 Thinking 后，每个事件仍只占一行，过长用 `...` 省略，工具名加粗。点击事件块的标题、正文或留白均可展开或收起；Enter/Space 也可切换，拖选和双击复制不受影响。
- Thinking 标题用动态星形图标显示思考、回复、工具执行和重试状态，并实时显示本次运行用时；思考时同时显示推理强度。并行工具结果按完成顺序展示，调用和结果各自独立折叠；原生搜索的结果和来源也支持收起。新会话保存工具结果到达顺序，旧记录按已有存储顺序恢复。结束后显示总耗时，中断和失败会单独标明。
- 向上滚动时保持当前位置；返回末尾后继续跟随新消息。点击输入框上方的 `↓ Latest message` 可跳到底部，此功能没有快捷键。对话和命令列表隐藏滚动条，仍可用滚轮与键盘滚动；命令介绍换行时保持介绍列对齐。累计 token 和估算上下文用量位于输入框上方、靠右显示；输入框下方的权限模式靠左显示，模型调用名和推理强度作为一组靠右显示，不显示 ready 等运行状态，不估算费用。
- 快捷键提示中的按键名称加粗。鼠标拖选正文或输入框文字后，松开即自动复制；双击或三击正文选中文字也会自动复制。复制成功时短暂显示紫底白字 `Copied`。macOS 本地直接写入系统剪贴板；其他终端使用 OSC52，需终端支持。密码输入框不自动复制。
- `/model` 每次打开都会并行读取已连接厂商的实时模型列表，再按厂商分组。Enter 进入该厂商的模型列表，Backspace 返回；↑/↓ 选择，←/→ 调整思考强度。使用现有账号或 API key 查询目录，不发送生成请求；加载中可按 Esc 取消。接口失败的厂商会显示原因，本次不展示其模型，也不回退到预设或旧列表；其他厂商仍可正常选择。`/effort` 打开 low → max 档位面板，用 ←/→ 调整。选中模型或思考强度后，Enter 保存为用户默认值、`s` 仅用于当前会话、Esc 取消。保存只修改对应设置；项目、环境变量和启动参数仍按原有配置优先级生效。带参数的 `/model provider/id` 和 `/effort high` 继续直接修改当前会话。
- 厂商内的模型优先按接口提供的发布或创建时间从新到旧排列；时间相同的保持厂商顺序，无时间字段的排在后面并保持厂商顺序。当前模型保留勾选标记与初始高亮，不额外置顶。厂商目录未提供时间时（如当前 Codex、DeepSeek 目录），展示的是厂商顺序，不能据此认定准确的发布日期先后。
- Shift+Tab 循环切换 `auto review`（黄棕色）、`accept all`（红色）、`plan`（蓝色）。权限请求在底部内嵌面板中纵向列出允许一次、拒绝、本会话允许，上方对话保持可见，输入草稿在确认后恢复。↑/↓（或 ←/→）选择、Enter 确认、Esc 拒绝，也可用 `y/n/a` 或 `1/2/3` 直接选择；默认选中拒绝，长问题可用 PgUp/PgDn 或滚轮查看。`-r` 无参数时使用全屏会话选择器，恢复后可以查看压缩前的历史。
- `/sessions` 或 Ctrl+R 打开所有工作空间的会话列表，`-r` 也使用这个列表。首次使用时按工作空间分组：工作空间按首次创建顺序从新到旧固定排列，组内会话按最后更新时间从新到旧排列；按 `g` 切换为所有会话按最后更新时间排列。切换后自动保存显示方式，重新启动 Virgo 时恢复上次选择。↑/↓ 移动，Enter 打开，Ctrl+X 将选中会话的文件移入系统回收站，`n` 在当前工作空间新建，Esc 返回。删除后留在列表中；若删除当前会话，会在原工作空间新建空会话。打开其他工作空间的会话会加载该目录的配置、扩展和工具环境。任务运行、命令排队或等待审批和输入时，Ctrl+R 都会立即打开列表，已有任务和队列继续执行；Esc 返回并保留草稿、审批和输入状态。无需等待当前工作结束即可按 `n` 或输入 `/new` 新建对话，也可打开其他会话；原任务继续在后台执行，切回后恢复消息、草稿、审批和输入状态。各会话的运行与中断独立，退出 Virgo 时统一停止。列表中的状态会自动更新；仍在运行或等待用户操作的会话不能移入回收站。

| 按键 | 作用 |
|---|---|
| Enter | 发送输入；运行中发送 steering 消息 |
| Shift+Enter | 换行；macOS 自带 Terminal 保留 Ctrl+J，见下方说明 |
| Tab | 补全 `/` 命令和 prompt 模板 |
| Shift+Tab | 循环切换三种权限模式，仅影响当前会话 |
| ↑ / ↓ | 命令列表打开时切换高亮项（默认选中第一项）；否则在输入中移动，到首尾时切换输入历史；Ctrl+P / Ctrl+N 可直接切换历史 |
| Ctrl+O / Ctrl+T | 展开或收起工具详情 / 思考 |
| Ctrl+R | 浏览所有工作空间的会话；列表内 `g` 切换分组和平铺 |
| Ctrl+X | 在会话列表中将选中会话移入系统回收站 |
| PageUp / PageDown | 滚动对话 |
| Ctrl+Home | 跳到对话开头 |
| Esc | 先关闭补全或弹层，否则中止运行；审批弹层中表示拒绝 |
| Ctrl+C | 运行时中止；空闲时清空输入；空输入下两秒内连按两次退出 |
| Ctrl+D | 输入为空时退出 |

Shift+Enter 需要终端发送可区分的修饰键序列。macOS 自带 Terminal 会将 Shift+Enter 和 Enter 都发为回车，Virgo 无法区分；此时保留 Ctrl+J 换行，底部提示也显示 Ctrl+J。不更改终端设置。

plain 模式保留逐行输入和原有行为：`Ctrl-C` 中止运行，空闲时两秒内连按两次退出。

退出时会先停止后台任务并清理会话。如果关闭钩子仍在等待，可以按 `Ctrl+C` 返回终端。
本次打开或新建的会话若没有消息、压缩记录或扩展状态，退出时会移除其记录；仅切换模型的空会话也会移除。工作空间创建顺序单独保存，更新会话或清理空会话不会改变顺序。旧工作空间首次使用此版本时，按现有最早会话的创建时间推定。

### 命令

| 命令 | 作用 |
|---|---|
| `/help` | 列出命令与 prompt 模板 |
| `/login [auth\|api] [provider]` | 账号登录或配置 API；无参数进入选择器 |
| `/logout [provider\|all]` | 选择并删除已保存的登录，或退出全部账号 |
| `/model [p/id]` `/effort [lvl]` | 查看或切换模型、推理强度 |
| `/shell [name\|path\|auto]` | 探测并选择命令解释器，保存用户默认值，并同步模型可见的 shell 信息 |
| `/search [auto\|duckduckgo\|deepseek\|off]` | 选择并保存搜索后端；无参数进入选择器，支持 `ddg`、`ds` 简写 |
| `/permissions [mode]` | 查看规则 / 切换权限模式 |
| `/compact [focus]` | 立即压缩上下文 |
| `/new` `/sessions [n]` `/session` | 新建、浏览、恢复会话和查看当前会话信息 |
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

使用 `/shell` 探测并选择 Bash、Zsh、sh/dash、PowerShell 7 或 Windows PowerShell 5.1；列表显示版本、路径和当前选择。也可用 `/shell pwsh`、`/shell zsh`、`/shell "C:\Program Files\PowerShell\7\pwsh.exe"` 或 `/shell auto`。选择立即生效并保存用户默认值；重启时项目、Runtime 配置及 `VIRGO_SHELL` 仍有更高优先级。Windows 自动优先 PowerShell，macOS/Linux 优先 Bash。显式配置失效时提示重新选择，不静默更换解释器。模型会收到实际 shell 类型、版本、路径和语法信息；已运行的后台任务保留启动时的 shell。

Bash/Zsh 开启 `errexit` 和 `pipefail`；PowerShell 7.4+ 对 cmdlet 和原生命令启用失败即停止。Windows PowerShell 5.1 / PowerShell 7.0–7.3 仅自动传播最后一个原生命令退出码，需逐条检查 `$LASTEXITCODE`；工具描述会提醒模型。PowerShell 使用独立 `powershell(...)` 权限规则，`plan` 模式不提供该工具。Windows 10+ 用 Job Object 管理命令及其后代进程，前后台输出直接落盘，展示和读取只保留有限尾部。完整行为、探测范围及兼容性见 [Shell 选择与执行](docs/SHELLS.md)。

```json
{
  "model": "anthropic/claude-opus-5-5",
  "effort": "medium",
  "shell": {"executable": "auto"},
  "permissions": {"mode": "auto-review", "allow": ["bash(uv run pytest*)", "edit(src/**)"], "deny": ["bash(git push*)"]},
  "providers": {
    "deepseek": {"api": "openai", "baseUrl": "https://api.deepseek.com", "apiKeyEnv": "DEEPSEEK_API_KEY",
                 "models": {"deepseek-flash": {"contextWindow": 1000000}}}
  },
  "hooks": {"PreToolUse": [{"matcher": "bash", "command": "./scripts/guard.sh"}]},
  "compaction": {"triggerRatio": 0.8, "keepRecentTokens": 20000, "triggerTokens": null},
  "retry": {"maxRetries": 3, "fallbackModel": null},
  "features": {"team": true, "schedule": true}
}
```

- **模型提供方**：Anthropic、OpenAI 兼容和 xAI Responses 使用官方 SDK；OpenAI 账号与 Google 账号使用对应的流式接口。也可以在 `providers` 里声明自定义端点。
- **小模型**：权限审批、其他 Provider 的压缩摘要、goal 评审和记忆整理共用自动选择的小模型。OpenAI API、Codex 账号及指向 OpenAI 官方 API 的自定义连接使用同一连接下的 `gpt-6-luna`；Anthropic 及指向其官方 API 的自定义连接使用同一连接下的 `claude-haiku-5-5`；其他厂商使用主模型。切换模型或登录后同步更新，旧 `smallModel` 配置不再参与选择。小模型默认优先使用 `low`；不支持时使用合法的模型默认强度，没有合法默认值时省略参数并记录原因。非推理模型不发送强度参数。此规则不受主对话 `/effort` 影响。实时目录提供强度信息时优先使用，也可在 `providers.<厂商>.models.<模型>` 中补充 `supportedReasoningLevels` 和 `defaultReasoningLevel`。
- **上下文压缩**：所有 Provider 默认在上下文估算达到模型窗口的 80% 时压缩；`triggerRatio` 可调整比例，非空 `triggerTokens` 优先指定绝对阈值，旧 `reserveTokens` 不再参与计算。OpenAI API 默认调用 [Responses 原生压缩](https://developers.openai.com/api/docs/guides/compaction)，压缩后使用 Responses 回传完整加密上下文；Codex 账号使用 Responses 的原生 `compaction_trigger`。Anthropic 默认调用 [按需压缩](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand)，使用当前模型并原样保留签名块。其他 Provider（包括第三方兼容 API）继续使用小模型结构化摘要。`keepRecentTokens` 控制 Anthropic 和通用摘要保留的近期消息；OpenAI 使用服务端返回的完整窗口。`/compact [focus]` 的重点提示适用于 Anthropic 和通用摘要，OpenAI 使用服务端摘要指令。原生请求失败时保留原上下文并报告错误；切换到不兼容 Provider 时从完整历史重建。
- **实时模型目录**：[OpenAI](https://developers.openai.com/api/reference/resources/models/methods/list)、[DeepSeek](https://api-docs.deepseek.com/api/list-models/) 及兼容连接查询其 API 根路径下的 `GET /models`；[Anthropic](https://platform.claude.com/docs/en/api/models/list) 查询 `GET /v1/models` 并处理分页；Codex 账号沿用[官方客户端的目录接口](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/endpoint/models.rs)。服务端明确隐藏、不可用或不适用于文本对话的模型会被过滤。`providers.<厂商>.models` 仅补充模型参数，不再决定菜单中有哪些模型；服务端返回的上下文和输出限制优先用于当前会话。Google Code Assist 账号目前没有适配的实时目录，需使用 `/model provider/id` 指定模型；离线 `faux` 演示继续使用本地目录。
- **目录版本更新**：Codex 从本机 `codex --version`、官方 `@openai/codex` 稳定发布元数据和上次成功的版本中选择最新候选，不使用 Virgo 的应用版本。发布元数据每 24 小时更新一次；目录为空或返回 HTTP 400/426 时强制刷新，至多再尝试一个候选版本。版本源无法连接时可使用本机或缓存版本继续查询；没有任何候选或重试仍失败时显示原因。版本信息保存在 `~/.virgo/cache/codex-catalog-version.json`（跟随 `VIRGO_HOME`），模型列表仍然每次实时获取。该流程只查询版本信息；固定 `providers.openai-codex.clientVersion` 会关闭自动选择与重试，删除该设置可恢复自动模式。
- **其他厂商的版本**：OpenAI API、DeepSeek 等兼容目录不需要 Codex 客户端版本参数；Anthropic 目录的 `anthropic-version` 跟随已安装 SDK 的默认值，升级 SDK 后同步更新，也可用 `providers.<厂商>.apiVersion` 固定。API 日期代表[接口协议版本](https://platform.claude.com/docs/en/api/versioning)，与模型列表更新分开管理。
- **权限规则**：写法是 `tool(pattern)`，pattern 为 fnmatch 风格，匹配命令或路径。例如 `bash(npm test*)`、`edit(src/**)`、`read(~/.ssh/*)`、`mcp__github__*`。
- **权限模式**：`auto-review` 自动放行可确认在工作目录内的操作；外部路径、联网及无法静态确认边界的操作由小模型审批。审批分别判断风险和用户授权，返回 `allow`、`ask` 或 `deny`：允许后执行；证据不足或审批失败时使用现有人工确认；明确拒绝时阻断并把理由交给主 agent，只能采取实质更安全的方案或询问用户。同一授权上下文中的精确被拒操作不能反复送审；拒绝后其他执行、修改类操作也会复审，避免换一种命令写法便按工作区规则放行，只读调查仍可继续。新用户消息或权限规则改变后重新评估。`accept-all` 跳过权限审批和 allow/deny 规则。`plan` 仅提供只读工具和命令，要求调查后给出方案。旧配置 `default`、`accept-edits` 自动映射到 `auto-review`，`yolo` 映射到 `accept-all`。

审批输入包含当前完整操作及发起 agent 所属分支的公开多轮轨迹：原始用户消息、助手公开文本、工具调用与结果、审批记录。子 agent 的委派指令、摘要和工具输出不会作为真实用户授权；隐藏思考及原生推理块不进入审查。输入总上限为 40,000 字符，优先缩短大段工具输出、移除旧工具交互，用户要求和人工审批按完整条目保留；缺失授权历史会标记并要求人工确认。会话压缩后仍可从当前分支回取原文，后台任务和子 agent 继续绑定原任务，避免切换聊天后串用上下文或记账。执行前会复核参数、路径边界、权限规则和用户授权上下文。

权限审批请求显式设置 **4096 token 总输出上限**（包括推理），不叠加通用摘要调用的 4096 token 推理余量。DeepSeek、Anthropic、OpenAI Chat Completions 和 Responses 等适配器发送此上限；Codex 账号适配器目前不发送输出上限，日志标记 `token_limit_supported=false`，不能保证其服务端遵守 4096。审批超时、截断或格式错误分别记录为审查失败，不等同于操作被判危险。其他辅助任务保留各自原有预算。审批属于执行前检查，当前没有 OS 级运行时沙箱。
- **命令钩子**：支持 `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop` 四种。钩子从 stdin 收到 JSON；退出码 2 表示拦截（或要求继续），stderr 的内容作为反馈。
- **功能开关**：每个内置功能都可以用 `features.<name>: false` 关闭（例如 `team`、`mcp`、`memory`）。
- **上下文文件**：`~/.virgo/AGENTS.md`，加上从根目录到 cwd 每一层的 `AGENTS.md` 或 `CLAUDE.md`。`SYSTEM.md` 替换身份段落，`APPEND_SYSTEM.md` 追加到末尾。

## 联网搜索

默认自动选择：优先使用当前模型已适配的官方原生搜索，没有可用适配时使用免费的 DuckDuckGo。支持 Anthropic Messages、OpenAI Responses（官方 Chat Completions 连接仅在原生搜索请求时改用 Responses）、OpenAI Codex 账号、xAI Responses，以及 DeepSeek 官方 Search API。使用 DeepSeek 官方连接时，内置 `web_search` 调用它的独立搜索接口；其余厂商的原生搜索由模型服务端执行。Google Code Assist、其他兼容服务及未知模型默认使用 DuckDuckGo。按实际端点和模型判断能力，不会仅凭 Provider 名称向代理地址发送原生工具。辅助审批、摘要和压缩请求不启用搜索。

输入 `/search` 打开选择器；plain 界面使用编号菜单。也可以直接指定：

```text
/search auto        # 模型原生搜索优先，没有可用适配则使用 DuckDuckGo
/search duckduckgo  # 始终使用免费的 DuckDuckGo，也可写 /search ddg
/search deepseek    # 始终使用 DeepSeek 官方搜索，也可写 /search ds
/search off         # 关闭联网搜索
```

选择会立即生效并保存为用户级 `settings.json` 中的默认值，不改变当前对话模型；重启时仍以项目和运行参数的覆盖设置为准。DeepSeek 搜索可供任意对话模型使用；选择前会检查 API 配置，缺少密钥时保留原选择，并提示使用 `/login api deepseek` 或设置 `DEEPSEEK_API_KEY`，配置后重新选择。搜索复用 DeepSeek 官方连接的已保存 API key 或环境变量，每次调用读取最新凭据，不把密钥写入搜索设置。自动模式下 DeepSeek 搜索缺少可用配置时使用 DuckDuckGo。旧版 Brave 配置会在加载时转为 DuckDuckGo，并忽略原有的搜索密钥变量；下一次使用 `/search` 时保存迁移后的选择。

以下是默认配置；`/search auto` 保存 `mode: "auto"` 和 `backend: "duckduckgo"`，手动选择某个后端保存 `mode: "builtin"` 和对应 `backend`：

```json
{
  "search": {
    "mode": "auto",
    "backend": "duckduckgo",
    "timeout": 15,
    "maxUses": 3
  }
}
```

- `mode`：`auto` 原生优先并允许回退；`native` 只用当前模型的原生搜索（包括 DeepSeek 官方 Search API），不可用时报错；`builtin` 只用指定的内置后端；`off` 关闭。`features.search: false` 也可完全停用该功能。
- `backend`：支持免密钥的 `duckduckgo` 和使用官方 API 的 `deepseek`。DeepSeek 搜索的独立密钥环境变量可通过 `search.apiKeyEnv` 指定。两种后端都复用 `httpx`，无需浏览器或常驻进程。
- `timeout`：内置搜索的超时秒数，默认 15，最高 30。工具参数为 `query` 和可选 `limit`（默认 5，最多 10）；DeepSeek 查询最多 128 个 Unicode 字符，DuckDuckGo 最多 2000 字符，超出限制会提示缩短查询。结果包含查询、检索时间、标题、网址和摘要，响应最大 1 MiB。
- `maxUses`：Anthropic 每轮原生搜索次数上限，1–10，默认 3；其他厂商保留其原生限制。Anthropic 暂停的搜索最多自动续接三次，随后由用户继续。

每次请求只启用一种搜索路径。只有明确的“原生搜索工具不支持”错误才触发一次回退，并按端点、模型和账号在当前会话缓存；认证失败、限流、超时、空结果或模型未搜索不会被误判为不支持。已经开始的服务端搜索遇到请求错误时会保留部分证据并停止，不自动重跑收费操作。原生来源链接会显示在终端，历史中保留原始引用；切换 Provider 后保留可读来源，不传递另一厂商的内部搜索对象。

联网权限沿用现有规则：内置工具按实际查询审批，DeepSeek 官方搜索也走这条路径；其他服务端原生搜索由厂商内部决定查询，`auto-review` 会对当前用户请求的搜索范围审批一次并缓存。范围无法获批时，`auto` 改用内置工具逐条审批。带查询条件的权限规则或自定义工具钩子也使用内置路径，以保留拦截能力。例如 `web_search(*private*)` 匹配查询内容，`web_search` 匹配所有搜索。`accept-all` 跳过权限审批；`plan` 允许只读搜索，但仍遵循 deny 规则。

可用 `providers.<名称>.nativeSearch: false` 关闭该连接的原生搜索。对已确认兼容的 Anthropic 或 Responses 端点，可设为 `true` 显式启用；普通自定义 Chat Completions 连接需先配置为 `api: "openai-responses"`，不会自动探测新协议。DeepSeek 搜索和其他原生服务按厂商规则计费；DuckDuckGo 网页搜索可能限流或要求浏览器验证，工具会明确报错，不把验证页当作搜索结果。

接口依据：[OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search)、[Codex web search](https://developers.openai.com/codex/web-search)、[Anthropic web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)、[xAI web search](https://docs.x.ai/developers/tools/web-search)、[DeepSeek Search API](https://api-docs.deepseek.com/api/search/)、[DuckDuckGo HTML 搜索](https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript)。

## 扩展

### 运行审计与用量

会话 JSONL 的 `custom` 记录保存 `permission_decision`（工具调用 ID、参数哈希、权限模式、判定来源、结论、理由、耗时及审查证据 ID/裁剪信息）、`model_call`（辅助请求的模型、期望/实际推理强度、回退原因、实际发送的预算、停止原因、耗时和 token）及 `goal_evaluation`（评审结论、证据哈希和截断标记）。这些审计记录不进入主对话模型上下文，也不重复保存内部提示词、工具参数或模型思考；权限审查可引用同分支的审批记录。人工确认、规则放行、审批失败和执行前重新审批也会留下记录；审批与模型调用通过工具调用 ID 关联。

界面累计用量包含当前分支的主对话、审批、Goal、记忆整理、Workflow、子 Agent、摘要和原生压缩，以及已经重试的失败请求。`↑` 是全部输入 token，包含系统提示词、项目指令、工具定义、历史消息及缓存读取/写入；`↓` 是输出 token。`cache` 显示累计缓存命中率：缓存读取 token ÷ 全部输入 token，缓存写入计入分母但不算命中；尚无输入时显示 `0.0%`，不再单列缓存 token 数。

恢复会话后继续累计，压缩不会清零；上下文百分比仍只计算主对话。新对话尚未发送消息时，上下文占比显示 `0.0%`，不提前估算系统提示词和工具定义；输入框中的未发送草稿不计入占比。发送后或压缩后没有可用 usage 时，上下文按系统提示词、工具定义和消息内容估算；有有效 usage 后直接采用服务返回的总量，避免重复计入系统提示词。已进入主对话但尚无 usage 的消息只计入上下文估算，不计入累计用量。旧会话只能汇总原有记录，无法补回历史上未记录的辅助请求用量。累计数字以模型服务实际返回的 usage 为准。

Goal 默认保留最多 100,000 字符证据，每段最多 12,000 字符；截断时同时保留首尾并明确标记遗漏。评审要求对照工具结果核验最终回复和已落盘文件，发现缺少证据时要求主 Agent 补查。它仍是无工具的模型评审，不替代实际文件检查或测试。Agent 指引同时要求读回产物、核对不同格式的字段，并保留检索来源和筛选依据。

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
| 权限 | `features/permission.py` | 三种模式；auto-review 识别工作区边界并对外部/联网操作调用小模型审批；accept-all 全部放行；plan 只读；无人值守时需要人工审批的操作视为拒绝 |
| 钩子 | `harness/extensions.py` | 带优先级的事件总线：`tool_call` 可拦截或改参，`tool_result` 可改结果，`agent_stop` 可要求继续；另有 shell 命令钩子 |
| TodoWrite | `features/todo.py` | 整表替换、校验失败不改状态；允许多个进行项，不限制条目数量；状态存在工具结果 `details` 里，可从会话重建，`/todos` 查看；未完成项不会触发续接 |
| 主动提问 | `features/ask_user.py` | 只给主 agent；模型可给 0–3 个选项，第一个为推荐项，末尾自动追加“自定义”输入；不给选项时直接显示输入框。非用户发起的运行（定时任务、后台任务完成、队友消息唤醒）在 `askUser.unattendedTimeout` 秒后（默认 300）收回问题：`answered=false`、`timed_out=true`，不填写答案；用户发消息或作答后取消时限。取消和 `-p` 等非交互模式也只报告未回答，不代选 |
| 子 Agent | `features/subagent.py` | 全新上下文，只返回最终答案；不能递归；`general` / `explore` 两种类型，可在 `agents/*.md` 自定义；可并行、可后台 |
| 技能 | `features/skills.py` | 渐进披露：prompt 只列目录，模型用 `read` 加载正文；`/skill:` 强制使用 |
| 上下文压缩 | `harness/compaction.py`、`ai/compaction.py`、`tools/truncate.py` | 默认 80% 窗口触发；OpenAI / Anthropic 原生压缩，其他 Provider 用自由结构的增量摘要；已有文本完整序列化后按总预算首尾裁剪，保留遗漏标记；完整历史保留，支持 `/compact` 和超限补救 |
| 记忆 | `features/memory.py` | 用户级和项目级两层，每条记忆一个 Markdown 文件，各配一个 `MEMORY.md` 索引；会话开始时仅把非空索引及读取入口放进 prompt；`memory` 工具主动保存并去重；`/memory tidy` 整理，失败回滚 |
| 任务系统 | `features/tasks.py` | 持久化依赖图 + 文件锁；认领时校验依赖；做环检测；完成时报告新解锁的任务；与队友共享 |
| 后台任务 | `features/background.py` | bash 和子 agent 共用一个 JobManager；完成后通知（agent 运行中则插入，空闲则唤醒） |
| 定时任务 | `features/schedule.py` | 5 字段 cron 加 `at`（ISO 时间或 `+10m`）；到点作为 follow-up 送达；可持久化 |
| Agent 团队 | `features/team.py` | 队友各占一个线程，有独立会话和进程内邮箱；完成当前委派后等待消息；模型通过 `task_list` / `task_update` 显式认领，`team_message` 委派或唤醒；仅设置 owner 不唤醒；停止和计划审批都是带 request_id 的协议；计划审批复用权限的 plan 模式；可为队友建独立 git worktree |
| MCP | `features/mcp.py` | 真正的 stdio JSON-RPC 客户端（initialize → tools/list → tools/call）；工具命名 `mcp__server__tool`；默认需要授权 |
| 集成 harness | `harness/session.py` | 所有功能都以扩展身份接入同一个循环；负责重试、回退模型、反应式压缩和统一唤醒 |
| Workflow | `features/workflow.py` | 编排由脚本固定，模型只负责调用；结构化输出带校验和重试；journal 按调用内容哈希，续跑时已完成的调用直接命中 |
| Goal loop | `features/goal.py` | 用户通过 `/goal` 启用后，准备停止时由无工具评审按用户条件和现有证据判断：达成 / 不可能 / 未满足则继续；有上限；后台任务未完成时暂缓评审 |

## 设计取舍

默认主 Agent 保留 24 个工具和现有 `features.*` 开关。工具描述只通过工具定义提供，基础提示不再重复工具目录或规定统一工作流程。技能由模型按需选择、使用前读取；用户 `SYSTEM.md` / `APPEND_SYSTEM.md`、项目指令和资源目录仍保留。用户显式启用的 `/goal`、Workflow、Stop hook 和 `plan_first` 继续生效。扩展接口变更见 [扩展迁移说明](docs/EXTENSION_MIGRATION.md)。

- **发送给 API 的历史只追加。** 
  - system prompt 和工具集合在同一权限模式及 shell 选择内保持稳定；`/shell` 显式切换时更新工具与环境信息；切换到或离开 plan 时更新规划提示和可用工具；通用摘要压缩后重建基础上下文，原生压缩保持前缀配置稳定；
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

跨平台 PR/基线对比、运行方式及报告说明见 [CI 文档](docs/CI.md)。

`tests/` 的目录与 `src/virgo/` 一一对应（`agent/`、`ai/`、`ai/providers/`、`cli/`、`cli/tui/`、`features/`、`harness/`、`tools/`），新测试放进被测模块对应的文件。共享的测试替身和工具在 `tests/support/`，测试模块之间不互相导入。跨平台测试约定和已知失败分析见 [docs/TESTING.md](docs/TESTING.md)。驱动真实伪终端的测试带 `pty` 标记，日常迭代可以跳过：

```bash
uv run pytest -q -m "not pty"
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
