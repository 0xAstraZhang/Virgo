# Virgo

**在代码所在的地方，把任务做完。**

[English](README.md) · [简体中文](README.zh-CN.md) · [文档中心](docs/zh-CN/README.md)

Virgo 是一个轻量、可扩展的终端编程 Agent。你可以让它理解代码库、实现改动、调查故障或审查 diff。它能够读取和编辑文件、执行命令，并在持续对话中推进任务。

> Virgo 的沙箱是防止模型偶发误操作的**护栏**：它把写入限制在当前工作区内，保护私有凭据和 Virgo 项目配置，并在越界或高危操作前请求确认。它**不是**抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行 Virgo。

在一级支持平台上，Virgo 默认以 **Workspace-Edit** 启动，通过原生沙箱约束 Shell 命令，并在越界或高危操作前请求确认。选择 **No-Limit**（`--permission no-limit` 或 `/permissions no-limit`）则直接使用当前宿主账户权限。详见[权限模式与审批](docs/zh-CN/guides/permissions.md)与[执行边界与沙箱](docs/zh-CN/guides/sandbox.md)。

## 平台支持分级

| 平台 | 级别 | 默认模式与说明 |
| --- | --- | --- |
| Linux（`bubblewrap`） | 一级 | **Workspace-Edit**；CI 阻塞与性能门槛 |
| macOS（Seatbelt） | 一级 | **Workspace-Edit**；CI 阻塞与性能门槛 |
| 原生 Windows 11（标准 AppContainer） | 一级 | 已更新 `null.sys` 的版本默认 **Workspace-Edit**（非提权，无需管理员权限）；旧版 Windows 默认 **No-Limit** 并给出一次性提示。本机回环与 `$HOME` 工作区有如实说明的平台限制（见[详细说明](docs/zh-CN/guides/windows-sandbox.md)） |
| Windows + WSL2（Ubuntu / Linux） | 一级 | 通过 Linux `bubblewrap` 后端默认 **Workspace-Edit**；适合旧版 Windows、Linux 工具链或需要沙箱内访问本机回环服务的工作流（见 [WSL2 使用指南](docs/zh-CN/guides/wsl2.md)） |

## 为什么使用 Virgo？

- **留在熟悉的终端里。** 全屏对话、实时工具活动、可恢复的会话，以及适用于不同环境的逐行模式。
- **自由选择模型。** 登录账号、接入 API，或连接兼容的本地服务；在会话中切换模型和推理强度。
- **让复杂任务分工进行。** 将独立工作交给子 Agent，用后台任务推进较大的任务，同时保留主对话。
- **适应你的项目。** 用项目指令、技能、提示模板和 Python 扩展定制行为，并开箱内置 MCP 和 Todos 扩展。
- **选择界面语言。** 输入 `/language 中文` 或 `/language English`，切换界面提示、命令帮助和内置工具描述。Virgo 默认用你当前消息的语言回答，你也可以指定回复语言。

Virgo 的设计参考了 [Pi](https://github.com/earendil-works/pi)。模型适配、Agent 循环、执行层和会话运行时彼此分离，便于按需扩展。

## 开始使用

需要 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

进入 Virgo 后，输入 `/login` 登录支持的账号或配置 API。使用 API 密钥登录时，Virgo 从服务端返回的列表中自动选用模型，依次优先选用该服务商下的当前模型、预设模型和列表第一项。之后可用 `/model` 切换。只有从 **API → 自定义** 登录，且服务端表示不支持 `/models`（HTTP 404、405 或 501）时，才会提示手填模型 ID。

连接后，可以请 Virgo 解释项目的启动流程。

```text
解释这个项目的启动流程，并告诉我应该在哪里添加新命令。
```

想在连接模型前先体验界面？离线演示无需凭据（使用预设响应）：

```bash
uv run virgo --model faux/demo
```

## 融入你的工作方式

```bash
# 在其他项目中工作
uv run virgo -C /path/to/project

# 执行单次请求
uv run virgo -p "检查当前 diff 中的正确性问题"

# 继续最近的会话（也可用 -r <路径> 打开指定会话）
uv run virgo --continue

# 与其他程序集成
uv run virgo -p --format json "总结 README"
```

- **全屏界面**：`Enter` 发送消息，`Ctrl+J` 换行，`/` 浏览命令，`Shift+Tab` 循环切换权限模式，`Ctrl+T` 展开思考，`Ctrl+O` 展开工具详情，`Esc` 中断当前回复。点击工具调用行（或聚焦后按 `Enter`）即可展开结果。
- Virgo 的**推理强度**默认采用模型目录返回的档位。目录未给出默认值时使用 `high`。如果列表中没有 `high`，则使用模型支持的第一档。你可以通过 `/effort`、`--effort` 或设置文件中的 `effort` 指定档位。
- **会话与恢复**：`--continue` 打开当前工作区最近更新的会话文件。若最后一条记录不完整且缺少换行符，Virgo 会截断损坏尾部并保留之前的有效记录；若文件头或完整记录损坏，启动会停止并指出文件和行号。
- **状态与权限**：`/status` 显示会话 ID、模型、工作目录、token 用量和当前权限。`/permissions` 汇总当前模式与执行边界（`/permissions show` 或 `-p /permissions` 输出完整 JSON）；通过 `/permissions <mode>`、`--permission <read-only|workspace-edit|no-limit>` 或 `Shift+Tab` 切换模式。
- **错误诊断**：`/diagnostics` 列出保存在 `~/.virgo/logs/diagnostics.sqlite3`（或 `$VIRGO_HOME/logs/diagnostics.sqlite3`）中的最近十条错误。可用 `--before <cursor>` 翻看更早记录，或用 `/diagnostics <error-id>` 查看详情；分享诊断详情前请先检查内容。

## 文档导航

浏览完整[文档中心](docs/zh-CN/README.md)：

- **使用指南**：[权限模式与审批](docs/zh-CN/guides/permissions.md) · [执行边界与沙箱](docs/zh-CN/guides/sandbox.md) · [原生 Windows 沙箱](docs/zh-CN/guides/windows-sandbox.md) · [WSL2 使用指南](docs/zh-CN/guides/wsl2.md) · [内置扩展（Todos 与 MCP）](docs/zh-CN/guides/extensions.md)
- **开发与架构**：[架构概览](docs/zh-CN/development/architecture.md) · [执行接口](docs/zh-CN/development/execution-interface.md) · [原生执行与性能验收](docs/zh-CN/development/execution-acceptance.md)
- **架构决策**：[决策记录索引](docs/zh-CN/decisions/README.md)
- **示例与反馈**：[项目示例](examples/README.zh-CN.md) · [反馈问题](https://github.com/0xAstraZhang/Virgo/issues)

## 许可证

[MIT](LICENSE)。第三方许可与致谢见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。
