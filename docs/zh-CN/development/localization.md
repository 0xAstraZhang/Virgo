# 本地化

[文档首页](../README.md) · [English](../../en/development/localization.md)

## 用户可见行为

Virgo 内置 `en`（English）和 `zh-CN`（中文）。使用以下命令切换：

```text
/language 中文
/language English
```

不带参数的 `/language` 显示当前语言和已注册选项。名称及别名解析为标准代码，匹配不区分大小写，并允许用下划线代替连字符。内置别名包括 `zh`、`Chinese`、`简体中文`、`en-US` 和 `en-GB`。

启动优先级依次为内置默认 `en`、用户 `settings.language`、项目 `settings.language`，最后是运行时／`--language` 覆盖值。不存在 `VIRGO_LANGUAGE` 环境变量。例如：

```bash
uv run virgo --language zh-CN
uv run virgo --language English --help
```

斜杠命令只在用户配置中保存 `language`，保存成功后应用到当前会话。写入失败时活动语言不变。如果语言已成功切换，但自定义 UI 刷新失败，Virgo 记录 `ui.language_changed`，保留已经提交的偏好，不会误报为保存回滚。命令等待当前主任务结束，直接 API 调用会拒绝忙碌中的会话。重启时项目和运行时覆盖值仍可能优先。其他活动会话保持各自选择，新会话使用其生效配置，包括从控制器继承的显式覆盖值。

语言控制界面提示、内置命令描述、工具标签和 schema 中的自然语言描述，也会向模型提供回复语言偏好，用户明确要求的语言优先。已有对话内容、用户文件、标识符、枚举值、Provider／MCP 响应及任意扩展原文不会被自动翻译。

## 包结构与归属

公开 API 位于 [`virgo.i18n`](../../../src/virgo/i18n/__init__.py)。内置词条位于 [`locales/en`](../../../src/virgo/i18n/locales/en) 和 [`locales/zh_CN`](../../../src/virgo/i18n/locales/zh_CN)，每种语言均分为 `core`、`harness`、`ui`、`features`、`tools` 模块，并导出 `MESSAGES` 字典。

| API | 职责 |
| --- | --- |
| `LanguagePack(code, name, messages, aliases=())` | 标准代码、本地名称、词条映射与可接受别名 |
| `register_language(pack)` | 在会话启动前校验并注册新语言 |
| `available_languages()` | 返回已注册语言，供命令／界面展示 |
| `normalize_language(value)` | 解析名称、代码、别名，或拒绝不支持的值 |
| `Translator(language="en")` | 持有一份可变的语言选择 |
| `translator(key, **values)`／`translator.text(...)` | 查找并格式化词条 |
| `translator.has(key)` | 检查当前或回退词条 |
| `translator.set_language(value)` | 修改这一翻译器的选择 |
| `session.t`、`api.t`、`ctx.t` | 使用所属会话／工具上下文翻译 |
| `session.set_language(value, persist=True)` | 应用合法的会话变更，并可选择保存偏好 |

注册表保存不可变的语言包快照，当前语言不是进程全局开关。UI、后台回调和工具执行使用其所属翻译器，一个对话不能通过全局状态修改另一个对话的语言。未注入翻译器的独立上下文使用英文。

## 词条键与格式化

使用稳定、按语义命名的键，例如 `language.changed` 或 `tools.read.description`。完整句子通过命名占位符插值：

```python
# 英文词条
MESSAGES = {"example.saved": "Saved {count} files to {path}."}

# 中文词条
MESSAGES = {"example.saved": "已将 {count} 个文件保存到 {path}。"}

# 词条接入目录后，在调用处使用
text = session.t("example.saved", count=3, path=destination)
```

不要从显示文本构造键，也不要把句子拆成多个翻译片段。不同语言保持相同占位符名称。注册时校验格式，并在英文词条存在时比较占位符集合；占位符必须是命名标识符，转换标记只允许 `!s`、`!r` 和 `!a`。字面花括号使用 `{{` 和 `}}`，调用处需要提供全部必需值。

查找先使用当前语言，再回退英文。选定词条无法格式化传入值时，会尝试英文回退；若英文也无法格式化，仍会抛出参数错误。两者都没有时返回键本身，使接入遗漏可见。因此额外语言包可以先部分翻译并回退英文，但随包发布的两套词条应保持完整、同步。语法需要不同形式时使用不同键，当前格式化器不实现复数规则引擎。

权限决策和执行前复核使用结构化的边界键与参数，不依赖翻译后原因是否为空。需要审批时，空的边界译文会回退英文说明；它不能绕过审批，包括此前拒绝所要求的再次审批。

## 命令与工具

命令注册支持 `description_key` 和 `argument_hint_key`。获取命令时再解析翻译，因此切换语言不需要重新注册所有命令。字面命令名、处理器和并发行为不变，语言选项从注册表生成。

`AgentTool.i18n_key` 让工具显式选择元数据本地化。以 `tools.read` 为前缀时：

| 元数据 | 词条键 |
| --- | --- |
| 工具标签 | `tools.read.label` |
| 工具描述 | `tools.read.description` |
| 参数描述 | `tools.read.parameters.properties.path.description` |
| 嵌套 schema 文本 | 经由实际 schema 容器、以 `description` 或 `title` 结尾的路径 |

`i18n_values` 提供映射，或接收翻译器的 callable，用于实际 Shell 信息等动态标签／描述占位符。`localize_tool` 复制元数据和 schema，不修改共享工具常量。内置键只有在显式接入、且原文本与标准英文词条匹配时才翻译，以保留扩展明确覆盖的文案。自定义工具也可使用额外语言包提供的键，即使没有标准英文条目；缺少翻译时回退到工具原文。只翻译 schema 的说明性文本。属性名、执行函数及 `default`、`const`、`enum`、`examples`、`required` 中的值保持不变，即使这些数据内含名为 `description` 或 `title` 的字段。

模型请求组装时，扩展提供的元数据或格式化失败会产生 `i18n.tool` 诊断，并使用原始 `ToolSpec`。Token 估算使用相同回退，但不记录错误、显示通知或排入诊断，因此空闲时刷新状态栏不会增加日志或会话历史。本地化失败不会移除本来可用的工具。

工具结果在 Virgo 自行生成解释文本的位置通过 `ctx.t` 或 `api.t` 翻译。文件内容、命令输出和远程载荷属于数据，不应送入翻译词条。

## 新增语言

宿主应用可在创建 `AgentSession` 或启动 CLI 前注册新包：

```python
from virgo.i18n import LanguagePack, register_language
from virgo.cli.main import main

register_language(
    LanguagePack(
        code="fr",
        name="Français",
        aliases=("French",),
        messages={
            "language.description": "afficher ou changer la langue de l’interface",
            "language.current": "Langue : {language}. {usage}",
        },
    )
)

raise SystemExit(main())
```

这个用于说明接口的部分语言包会出现在 `/language` 中，并可通过 `--language fr` 选择；未翻译词条回退英文。随项目发布的完整新语言应提供与英文对应的领域字典，并在内置加载器中注册。UI 和命令分发不需要增加语言专用分支。重复代码或别名会被拒绝，不会替换已有包。

`register_language` 用于新增语言，不会修改已注册的中英文词条。额外语言包可以包含自定义工具键，其他语言缺少这些键时使用工具作者提供的标签／描述。扩展随项目发布的中英文词条时，在相应源码目录增加对应键。没有本地化元数据的扩展保留原文。

发布词条前，应检查键覆盖、占位符一致性、wheel 包含情况、启动优先级、保存失败、即时切换和独立对话。通过全屏及逐行交互确认结构化标识和原始内容未改变，使用[开发与 CI](testing-and-ci.md)中已有的端到端路径。
