# 使用 Virgo

[文档首页](../README.md) · [English](../../en/guides/interaction.md)

## 对话与语言

输入任务并按 Enter。Agent 工作时再次发送消息，会在当前批次工具执行结束后、下一次模型调用前补充到当前任务中。只读命令可并发执行；普通状态修改命令会等待当前运行结束。`/mode plan` 立即收紧尚未开始的调用。退出 Plan、修改计划或执行计划要求当前对话空闲；忙碌时的计划编辑和执行会被拒绝，不会排队。

```text
/language 中文
/language English
/language
```

前两条命令切换当前会话，并保存为用户默认值。`/language` 显示当前语言和可用选项。配置中的标准值为 `zh-CN` 和 `en`。语言设置影响 Virgo 自身的界面、命令描述和内置工具元数据；命令名、参数键、模型 ID 和机器可读值保持稳定。选定语言也会作为模型回复偏好，用户明确要求的语言优先。对话历史、文件内容和第三方输出保留原文。扩展接入方式见[本地化](../development/localization.md)。

## 命令

`/help` 列出当前配置下已注册的命令，包括提示模板。禁用插件后，相应命令不会注册。默认预设和开关见[配置](../reference/configuration.md#预设与插件)，退役命令见[迁移](migrating-to-core-plugins.md)。全屏编辑器在完整命令名后显示参数提示；输入参数后提示消失，也不会随输入一起提交。

| 命令 | 用途 |
| --- | --- |
| `/help` | 列出命令和提示模板 |
| `/language [中文\|English]` | 查看或切换界面语言 |
| `/login [auth\|api] [provider]` | 登录账号或接入 API |
| `/logout [provider\|all]` | 删除已保存的凭据 |
| `/model [provider/id]` | 选择模型或直接切换 |
| `/effort [low\|medium\|high\|xhigh\|max]` | 选择或设置推理强度 |
| `/shell [name\|path\|auto]` | 选择命令解释器 |
| `/search [auto\|duckduckgo\|deepseek\|off]` | 选择搜索方式 |
| `/fetch [auto\|http\|deepseek\|off]` | 选择网页读取方式 |
| `/permissions [ask\|never\|revoke]` | 查看规则、选择审批策略或撤销会话授权 |
| `/mode [default\|plan]` | 查看或切换 WorkMode，保持审批策略与 Sandbox |
| `/plan [show\|edit [Markdown]\|execute <id>:<revision>]` | 查看、替换或明确执行当前方案 |
| `/sandbox` | 查看配置的原生命令沙箱 |
| `/compact [focus]` | 压缩较早的模型上下文 |
| `/new`、`/sessions [number]`、`/session` | 新建、浏览或查看会话 |
| `/diagnostics [error-id]` | 查看近期错误和诊断记录 |
| `/review [uncommitted\|base <branch>\|commit <revision>\|show [id\|last]]` | 运行或查看[独立只读代码审查](code-review.md)；名称可用时 `/code-review` 提供同一入口 |
| `/todos`、`/jobs` | 查看当前对话清单或后台任务 |
| `/skills`、`/skill:name [args]` | 列出技能或显式使用技能 |
| `/mcp [reconnect]` | 查看或重新加载 MCP 连接 |
| `/exit` | 退出交互程序 |

模型和推理强度选择器读取已保存的模型目录，不会自行联网刷新。Virgo 启动时在后台刷新已连接的厂商；首次目录仍在加载时，可在完成后重新打开 `/model`。刷新失败会保留旧目录。选择器中 Enter 保存为用户默认值，`s` 仅应用于当前会话，Esc 取消。直接使用 `/model provider/id` 和 `/effort high` 只影响当前会话。

## 全屏快捷键

| 按键 | 操作 |
| --- | --- |
| Enter | 发送输入；在菜单中确认选中项 |
| Shift+Enter | 换行；macOS Terminal 使用 Ctrl+J |
| Tab | 补全斜杠命令和提示模板 |
| Shift+Tab | 切换 Default / Plan 工作模式；保持审批策略和 Sandbox |
| ↑ / ↓ | 在菜单中移动；在编辑器边界浏览输入历史 |
| Ctrl+P / Ctrl+N | 直接浏览输入历史 |
| Ctrl+G | 打开当前计划，查看、修改或明确执行 |
| Ctrl+O / Ctrl+T | 切换工具详情／思考的展开状态 |
| Ctrl+R | 打开会话列表 |
| PageUp / PageDown | 滚动对话或当前详情 |
| Ctrl+Home | 回到开头 |
| Esc | 关闭菜单；否则中止运行；在审批中拒绝 |
| Ctrl+C | 中止运行、清空输入；空输入时两秒内再按一次退出 |
| Ctrl+D | 输入为空时退出 |

macOS Terminal 可能将 Enter 和 Shift+Enter 发送为相同序列，因此界面提示使用 Ctrl+J 换行。其他终端也需要发送可区分的修饰键序列，才能使用 Shift+Enter。

消息正文保持展开；思考、工具调用和结果可独立折叠。向上滚动时停止跟随新输出，回到底部或点击最新消息控件后恢复跟随。选择文字并松开即可复制；密码字段不会自动复制。macOS 使用系统剪贴板，其他终端需要支持 OSC52。

审批面板默认选中拒绝。可以用方向键和 Enter、`y/n/a` 或 `1/2/3` 选择允许一次、拒绝或本会话允许。长问题可滚动查看。权限规则与各模式的含义见[配置参考](../reference/configuration.md#权限)。

底部状态栏分别显示 WorkMode、审批策略和实际采用的 Sandbox。窄终端优先保留这三项，再显示模型与推理强度。Plan 独立于 `ask` 或 `never`；`/sandbox` 还会报告配置边界与后端状态。

## 审阅计划后明确执行

使用 `virgo --work-mode plan` 启动，或在现有对话中输入 `/mode plan`。让 Virgo 调查并提出方案。Ctrl+G 打开计划 Markdown 和准确版本 token；`/plan` 也会通过命令输出显示这些内容。

修改计划窗口中的正文后，**执行此计划**按钮禁用。**保存修改**创建新版本，并保持 Plan；重新打开窗口即可审阅。**执行此计划**只执行窗口所显示的版本，其他入口已经更新该版本时会拒绝执行。关闭窗口不会执行计划或保存编辑。对话忙碌时，保存和执行会被拒绝，不会排队。

使用 `/permissions` 选择审批策略。执行计划或切换 WorkMode 都保留该策略与 Sandbox。完整操作、逐行终端编辑与恢复规则见[规划工作流](planning.md)。

## 会话与工作空间

`--continue` 打开当前工作目录最近的会话。`--resume [FILE]` 打开指定文件或显示选择器。全屏 `/sessions` 和 Ctrl+R 可浏览所有工作空间：

- Enter 打开会话，`n` 在当前工作空间新建，Esc 返回。
- `g` 切换工作空间分组和平铺列表，并保存选择。
- Ctrl+X 将已保存会话移入系统回收站；正在运行或等待输入的会话不能删除。

打开其他对话不会停止原任务。每个正在运行的对话分别保留草稿、待回答的问题、事件流和取消状态。打开其他工作空间时会加载该目录的配置、工具和扩展。退出 Virgo 会停止本应用中的活动任务。

会话以 JSONL 格式保存在用户数据目录。压缩只改变发送给模型的上下文，完整历史仍可回放。本次打开但没有内容的空会话会在退出时清理。保存失败后，进一步运行会暂停，尚未写入磁盘的历史保留在内存中；详见[故障排查](troubleshooting.md)。

逐行界面保留编号选择和逐行输入。Ctrl+C 中止当前运行；空闲时两秒内连按两次退出。

实现：[CLI](../../../src/virgo/cli)、[命令](../../../src/virgo/harness/commands.py)、[会话存储](../../../src/virgo/harness/store.py)。
