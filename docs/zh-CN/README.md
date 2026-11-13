# Virgo 文档

[项目首页](../../README.zh-CN.md) · [English](../en/README.md)

从你要完成的任务开始阅读。使用指南介绍日常操作；需要具体参数或实现约定时，再查阅参考和开发文档。

## 使用 Virgo

| 指南 | 内容 |
| --- | --- |
| [快速开始](guides/getting-started.md) | 安装、登录、首次会话、打印模式 |
| [使用 Virgo](guides/interaction.md) | 命令、快捷键、语言、会话和运行中补充指令 |
| [联网搜索与网页读取](guides/web.md) | 后端、能力路由、权限和读取限制 |
| [命令解释器](guides/shells.md) | Shell 选择、执行语义、输出和取消 |
| [定时任务](guides/schedules.md) | 创建、编辑、持久化、时间和重启行为 |
| [故障排查](guides/troubleshooting.md) | 错误、日志、诊断证据和恢复边界 |

## 查阅细节

- [配置参考](reference/configuration.md)：优先级、配置项、环境变量、权限和存储位置。
- [工具与自动化](reference/tools.md)：文件、任务、记忆、子 Agent、团队、工作流和 MCP。
- [示例文件](../../examples/README.zh-CN.md)：按需将单个文件复制到自己的项目。

## 扩展与开发

- [架构](development/architecture.md)：模块职责、执行流程和稳定边界。
- [扩展](development/extensions.md)：Python 工具和命令、事件、技能、提示模板和工作流。
- [本地化](development/localization.md)：语言包、会话归属、回退策略和新增语言。
- [开发与 CI](development/testing-and-ci.md)：本地命令、端到端验收、结果比较和运行器维护。

## 文档维护约定

`docs/en/` 与 `docs/zh-CN/` 按路径一一对应。文档说明当前行为，实现细节通过源码链接按需展开。命令、配置项、接口或路径发生变化时，在同一次改动中同步更新两种语言。特定版本的调查过程放在 issue 或 PR 中，可复用的结论整理进对应指南。根目录的两份 README 用于介绍产品并引导阅读。
