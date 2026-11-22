# Virgo 示例

[项目首页](../README.zh-CN.md) · [English](README.md)

这些文件展示了项目如何扩展 Virgo。按需将选中的部分复制到自己的项目，并调整路径和模型 ID；配置应与已有设置合并。

## 文件对应关系

| 示例 | 在项目中的目标位置 | 用途 |
| --- | --- | --- |
| [项目指令](project/AGENTS.example.md) | 项目根目录的 `AGENTS.md` | 随项目上下文读取的共享指令 |
| [配置](project/.virgo/settings.json) | `.virgo/settings.json` | 语言、推理强度、Shell及可选本地 API |
| [MCP 配置](project/.virgo/mcp.json) | `.virgo/mcp.json` | 内置 [MCP 扩展](../docs/zh-CN/guides/extensions.md)的默认禁用占位示例 |
| [敏感文件保护](project/.virgo/extensions/protect_env.py) | `.virgo/extensions/protect_env.py` | 工具钩子与 `/stats` 命令 |
| [审查模板](project/.virgo/prompts/inspect.md) | `.virgo/prompts/inspect.md` | 支持参数的 `/inspect` |
| [代码审查技能](project/.virgo/skills/code-review/SKILL.md) | `.virgo/skills/code-review/` | 技能与单独的检查清单 |

`project/.virgo/` 对应 Virgo 实际使用的项目配置目录。外层 `project/` 用于清楚展示示例，不属于目标路径。`AGENTS.example.md` 应放到项目根目录，而不是 `.virgo/` 内。

## 尝试单个示例

复制后在目标项目中启动 Virgo。例如在 POSIX Shell 中：

```bash
mkdir -p /path/to/project/.virgo/prompts
cp examples/project/.virgo/prompts/inspect.md /path/to/project/.virgo/prompts/inspect.md
```

如果希望审查模板提到的技能可用，还需要复制 code-review 技能。新增扩展、模板、技能或 Agent 类型后，重启 Virgo。

审查 Python 保护扩展、项目级 `providers` 或 `.virgo/mcp.json` 后，将其所在目录加入用户级 `extensions.trustedProjects` 或启动时传入 `--trust-project-extensions` 启用；未授信项目的扩展、MCP 服务器、钩子与自定义 provider 都会被忽略。

配置示例中的 `language` 会固定该项目的语言；中文使用 `zh-CN`，省略该字段则遵循用户默认值。可选的 `local` provider 需要真实服务、其安装的模型 ID／上下文窗口以及项目信任（`extensions.trustedProjects` 或 `--trust-project-extensions`），只有选择后才会更换当前模型。示例不再包含过时的 `smallModel` 覆盖项，辅助模型由运行时选择。

MCP 示例默认禁用，路径为占位内容。MCP 由内置 [MCP 扩展](../docs/zh-CN/guides/extensions.md)提供；替换为自己的 stdio 服务命令和参数并信任该项目后再启用。项目指令和提示文本属于作者提供的资源，`/language` 不会改写这些内容。
