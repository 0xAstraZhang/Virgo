# Virgo

**在代码所在的地方，把任务做完。**

[English](README.md) · [简体中文](README.zh-CN.md)

Virgo 是一个轻量、可扩展的终端编程 Agent。你可以让它理解代码库、实现改动、调查故障或审查 diff。它能够读取和编辑文件、执行命令，并在持续对话中推进任务。

Virgo 首次启动使用 **No-Limit**，直接使用当前宿主账户权限。**Read-Only** 与 **Workspace-Edit** 使用原生沙箱访问真实工作区，支持按具体操作申请额外权限。见[执行边界](docs/zh-CN/guides/sandbox.md)。

## 为什么使用 Virgo？

- **留在熟悉的终端里。** 全屏对话、实时工具活动、可恢复的会话，以及适用于不同环境的逐行模式。
- **自由选择模型。** 登录账号、接入 API，或连接兼容的本地服务；在会话中切换模型和推理强度。
- **让复杂任务分工进行。** 将独立工作交给子 Agent，用后台任务推进较大的任务，同时保留主对话。
- **适应你的项目。** 用项目指令、技能、提示模板和 Python 扩展定制行为，包括可选的 MCP 客户端。
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

使用 `/permissions show` 查看当前任务和下一任务的模式，使用 `/permissions` 在三种模式间切换。见[权限模式与审批](docs/zh-CN/development/permission-profiles.md)。

## 继续了解

| 我想…… | 从这里开始 |
| --- | --- |
| 安装 Virgo 并连接模型 | [快速开始](docs/zh-CN/guides/getting-started.md) |
| 了解命令、快捷键和会话 | [使用 Virgo](docs/zh-CN/guides/interaction.md) |
| 配置模型和权限 | [配置参考](docs/zh-CN/reference/configuration.md) |
| 添加技能、工具或插件 | [扩展 Virgo](docs/zh-CN/development/extensions.md) |
| 理解代码或参与开发 | [架构](docs/zh-CN/development/architecture.md) · [开发指南](docs/zh-CN/development/testing-and-ci.md) |

[全部文档](docs/zh-CN/README.md) · [English documentation](docs/en/README.md) · [示例](examples/README.zh-CN.md) · [反馈问题](https://github.com/0xAstraZhang/Virgo/issues)

## 许可证

[MIT](LICENSE)。第三方许可与致谢见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。
