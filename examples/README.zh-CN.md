# Virgo 示例

[项目首页](../README.zh-CN.md) · [English](README.md)

这些文件展示了项目如何扩展 Virgo。按需将选中的部分复制到自己的项目，并调整路径和模型 ID；配置应与已有设置合并。

## 文件对应关系

| 示例 | 在项目中的目标位置 | 用途 |
| --- | --- | --- |
| [项目指令](project/AGENTS.example.md) | 项目根目录的 `AGENTS.md` | 随项目上下文读取的共享指令 |
| [配置](project/.virgo/settings.json) | `.virgo/settings.json` | 语言、推理强度、Shell、权限及可选本地 API |
| [MCP 配置](project/.virgo/mcp.json) | `.virgo/mcp.json` | 默认禁用的 stdio 服务占位示例 |
| [敏感文件保护](project/.virgo/extensions/protect_env.py) | `.virgo/extensions/protect_env.py` | 工具钩子与 `/stats` 命令 |
| [审查模板](project/.virgo/prompts/review.md) | `.virgo/prompts/review.md` | 支持参数的 `/review` |
| [代码审查技能](project/.virgo/skills/code-review/SKILL.md) | `.virgo/skills/code-review/` | 技能与单独的检查清单 |
| [审查 Agent](project/.virgo/agents/reviewer.md) | `.virgo/agents/reviewer.md` | 只读子 Agent 类型 |

`project/.virgo/` 对应 Virgo 实际使用的项目配置目录。外层 `project/` 用于清楚展示示例，不属于目标路径。`AGENTS.example.md` 应放到项目根目录，而不是 `.virgo/` 内。

## 尝试单个示例

复制后在目标项目中启动 Virgo。例如在 POSIX Shell 中：

```bash
mkdir -p /path/to/project/.virgo/prompts
cp examples/project/.virgo/prompts/review.md /path/to/project/.virgo/prompts/review.md
```

如果希望审查模板提到的技能可用，还需要复制 code-review 技能。新增扩展、模板、技能或 Agent 类型后，重启 Virgo。

审查 Python 保护扩展后，用 `--trust-project-extensions` 启用；项目配置不能自行授予信任。

配置示例中的 `language` 会固定该项目的语言；中文使用 `zh-CN`，省略该字段则遵循用户默认值。可选的 `local` provider 需要真实服务及其安装的模型 ID／上下文窗口，只有选择后才会更换当前模型。示例不再包含过时的 `smallModel` 覆盖项，辅助模型由运行时选择。

MCP 示例默认禁用，路径为占位内容。替换为自己的 stdio 服务命令和参数后再启用。项目指令和提示文本属于作者提供的资源，`/language` 不会改写这些内容。

API 见[扩展](../docs/zh-CN/development/extensions.md)，优先级见[配置参考](../docs/zh-CN/reference/configuration.md)，运行行为见[工具与插件](../docs/zh-CN/reference/tools.md)。
