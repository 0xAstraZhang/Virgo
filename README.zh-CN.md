# Virgo

**在代码所在的地方，把任务做完。**

[English](README.md) · [简体中文](README.zh-CN.md)

Virgo 是一个轻量、可扩展的终端编程 Agent。你可以让它理解代码库、实现改动、调查故障或审查 diff。它能够读取和编辑文件、执行命令，并在持续对话中推进任务。

> Virgo 的沙箱是防止误操作的**护栏**：它把模型偶发失误的影响限制在当前工作区内，保护 Virgo 的私有数据和项目控制文件，并在越界或高危操作前请求确认。它**不是**抵御恶意代码、主动绕过行为或提示词注入攻击链的安全边界。处理不可信的仓库或内容时，请在容器或虚拟机中运行 Virgo。

在 Linux、macOS、WSL2 和原生 Windows 上，Virgo 默认以 **Workspace-Edit** 启动，通过原生沙箱约束 Shell 命令，并支持按操作申请额外权限。选择 **No-Limit**（`--permission no-limit` 或 `/permissions no-limit`）则直接使用当前宿主账户权限。原生 Windows 使用标准 AppContainer（无需管理员权限）；如果你偏好 Linux 工具链或需要在沙箱命令中访问本机回环服务（`127.0.0.1`），WSL2 同样是一级支持选项。见[执行边界](docs/zh-CN/guides/sandbox.md)、[原生 Windows 沙箱](docs/zh-CN/guides/windows-sandbox.md)与 [WSL2 使用指南](docs/zh-CN/guides/wsl2.md)。

## 平台支持分级

| 平台 | 级别 | 说明 |
| --- | --- | --- |
| Linux（`bubblewrap` + `libseccomp2`） | 一级 | 完整支持，默认 **Workspace-Edit**，CI 阻塞门槛与性能门槛 |
| macOS（Seatbelt） | 一级 | 完整支持，默认 **Workspace-Edit**，CI 阻塞门槛与性能门槛 |
| 原生 Windows（标准 AppContainer） | 一级 | 完整支持，默认 **Workspace-Edit**，无需管理员权限，会话级单 SID，CI 阻塞门槛与性能门槛；本机回环（`127.0.0.1`/`[::1]`）与 `$HOME` 工作区有如实说明的平台限制（见[详细说明](docs/zh-CN/guides/windows-sandbox.md)） |
| Windows + WSL2（Ubuntu / Linux） | 一级 | 使用 Linux 沙箱后端 — 适合 Linux 工具链或需要沙箱内访问本机回环服务的工作流（见 [WSL2 使用指南](docs/zh-CN/guides/wsl2.md)） |

## 为什么使用 Virgo？

- **留在熟悉的终端里。** 全屏对话、实时工具活动、可恢复的会话，以及适用于不同环境的逐行模式。
- **自由选择模型。** 登录账号、接入 API，或连接兼容的本地服务；在会话中切换模型和推理强度。
- **让复杂任务分工进行。** 将独立工作交给子 Agent，用后台任务推进较大的任务，同时保留主对话。
- **适应你的项目。** 用项目指令、技能、提示模板和 Python 扩展定制行为，包括可选的 MCP 和 Todos 扩展。
- **使用中文或英文。** 输入 `/language 中文` 或 `/language English`，切换界面提示、命令帮助和内置工具描述。

Virgo 的设计参考了 [Pi](https://github.com/earendil-works/pi)。模型适配、Agent 循环和会话运行时彼此分离，便于按需扩展。

## 开始使用

需要 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

进入 Virgo 后，输入 `/login` 登录支持的账号或配置 API，然后提出一个具体任务：

```text
解释这个项目的启动流程，并告诉我应该在哪里添加新命令。
```

想先体验界面？离线演示无需凭据：

```bash
uv run virgo --model faux/demo
```

演示使用预设响应；实际编程工作需要连接模型。

## 融入你的工作方式

```bash
# 在其他项目中工作
uv run virgo -C /path/to/project

# 执行单次请求
uv run virgo -p "检查当前 diff 中的正确性问题"

# 继续最近的会话
uv run virgo --continue

# 与其他程序集成
uv run virgo -p --format json "总结 README"
```

使用 `/permissions show` 查看当前任务和下一任务的模式，使用 `/permissions` 或 `--permission <read-only|workspace-edit|no-limit>` 在三种模式间切换。见[权限模式与审批](docs/zh-CN/development/permission-profiles.md)。

## 查看错误记录

用 `/diagnostics` 查看最近十条错误和数据库路径。用
`/diagnostics --before <cursor>` 翻到更早的记录，或用
`/diagnostics <error-id>` 按完整 ID 或唯一前缀查看详情。

错误保存在 `~/.virgo/logs/diagnostics.sqlite3`（若设置了 `VIRGO_HOME`，
则位于 `$VIRGO_HOME/logs/diagnostics.sqlite3`），重启后仍可查询。
该命令会增量导入旧的 JSONL 日志并保留源文件。分享诊断详情前请先检查内容。

## 继续了解

[执行边界](docs/zh-CN/guides/sandbox.md) · [WSL2 使用指南](docs/zh-CN/guides/wsl2.md) · [安装可选扩展](extensions/README.zh-CN.md) · [示例](examples/README.zh-CN.md) · [反馈问题](https://github.com/0xAstraZhang/Virgo/issues)

## 许可证

[MIT](LICENSE)。第三方许可与致谢见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。
