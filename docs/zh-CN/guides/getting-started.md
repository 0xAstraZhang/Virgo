# 快速开始

[文档首页](../README.md) · [English](../../en/guides/getting-started.md)

## 从源码安装

Virgo 需要 Python 3.11 或更新版本。仓库使用 [uv](https://docs.astral.sh/uv/) 管理解释器、依赖和锁文件。

```bash
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

安装后的入口命令是 `virgo`，也可使用 `uv run python -m virgo`。若希望在仓库之外直接使用命令，在仓库根目录运行 `uv tool install .`，之后通过 `virgo -C /path/to/project` 启动。

`uv run virgo --help` 显示命令行参数，`uv run virgo --version` 显示安装的版本。修改项目时需要的检查见[开发环境](../development/testing-and-ci.md)。

## 连接模型

在 Virgo 中输入 `/login`，选择账号认证或 API 配置。登录流程在界面内完成，并保存连接配置和选定的模型，供后续会话使用。

| 方式 | 可用预设 | 接入方式 |
| --- | --- | --- |
| 账号（`auth`） | OpenAI、Anthropic、xAI、Google、Kimi | 按厂商使用浏览器、授权码或设备码流程 |
| API（`api`） | DeepSeek、GLM、Kimi | 预设服务地址，加上你提供的 API key |
| 自定义 API | 自行指定 provider ID | OpenAI 兼容或 Anthropic Messages 接口、模型 ID 和密钥 |

例如：

```text
/login auth openai
/login api deepseek
/login api custom
/logout
```

按你实际使用的服务地址和账号完成登录。账号可登录并不表示该账号拥有所有模型或 API 产品的权限。Google 登录可能要求 Cloud project ID，也会读取 `GOOGLE_CLOUD_PROJECT` 和 `GOOGLE_CLOUD_PROJECT_ID`。

也可以继续使用环境变量凭据。例如，在启动 Virgo 前设置 `ANTHROPIC_API_KEY`，即可使用内置 Anthropic 连接。其他厂商与自定义端点的设置见[配置参考](../reference/configuration.md)。

凭据单独保存在 `~/.virgo/auth.json`，设置 `VIRGO_HOME` 后使用该目录。密码输入不会进入聊天或输入历史。保存的凭据绑定登录时的接口协议和地址；修改这些连接信息后需要重新登录。`/logout` 删除本地保存的凭据，环境变量中的凭据仍然有效。

## 开始对话

```bash
uv run virgo -C /path/to/project
```

当标准输入、输出均为交互终端且终端受支持时，Virgo 打开全屏界面；否则使用逐行界面。也可手动选择：

```bash
uv run virgo --ui plain
uv run virgo --ui fullscreen
```

终端不支持全屏时，`--ui fullscreen` 会明确报错。进入对话后，用 `/help` 查看命令，用 `/language 中文` 或 `/language English` 切换界面语言。也可使用 `uv run virgo --language zh-CN` 指定启动语言。

建议先尝试一个小任务，例如“找到程序入口，解释命令是如何注册的”。Virgo 工作时仍可补充指令；[使用 Virgo](interaction.md)介绍了运行中补充指令、会话切换和取消操作。

## 执行单次请求或接入脚本

```bash
uv run virgo -p "总结 README.md"
uv run virgo -p --mode json "列出公开的程序入口"
git diff | uv run virgo -p "检查这份 diff 中的正确性问题"
```

文本打印模式输出最终答案。JSON 模式输出 JSONL 事件；有显式结果的斜杠命令会产生 `command_result` 记录。`-p` 必须通过参数或标准输入获得提示。此模式不能回答交互问题，相关工具会报告未作答，不会替你选择。

`--no-session` 关闭对话历史保存，但不会关闭诊断日志或工具产生的输出文件。`--no-extensions` 跳过用户和项目的 Python 扩展；官方插件由 `--profile` 和 `plugins` 配置控制。`--profile minimal --no-extensions` 只启用 Core 能力；默认 `coding` 保留标准编程工具与可选助手能力。项目 Python 扩展需要 `--trust-project-extensions` 或用户级信任，见[迁移](migrating-to-core-plugins.md)。

## 体验离线演示

```bash
uv run virgo --model faux/demo
uv run virgo -p --model faux/demo "find python files"
```

`faux` 提供确定性的演示响应，支持无需模型账号的本地检查。它不是通用编程模型。

下一步：[命令与会话](interaction.md)、[配置](../reference/configuration.md)或[示例](../../../examples/README.zh-CN.md)。
