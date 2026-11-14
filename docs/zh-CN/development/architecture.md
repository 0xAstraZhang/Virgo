# 架构

[文档首页](../README.md) · [English](../../en/development/architecture.md)

Virgo 将模型传输、Agent 循环和应用策略分离。内置功能与项目扩展使用相同的扩展 API。模型选择完成任务的方法，Harness 提供能力、执行边界、持久化和可观察信息。

## 仓库结构

| 路径 | 职责 |
| --- | --- |
| `src/virgo/ai/` | 统一消息、流式接口、认证、模型元数据和厂商适配器 |
| `src/virgo/agent/` | 工具调用循环、事件、取消、steering 和 follow-up 队列 |
| `src/virgo/harness/` | 会话／Runtime 构造、配置、命令、扩展、上下文、权限和持久化 |
| `src/virgo/tools/` | 文件、Shell 和网页执行基础能力 |
| `src/virgo/features/` | 通过 `setup(api)` 注册的内置功能 |
| `src/virgo/cli/` | 入口、打印模式、逐行 REPL 和 Textual 全屏界面 |
| `src/virgo/i18n/` | 语言注册表、翻译 API 和内置词条 |
| `tests/` | 按源码职责组织的测试，共享支持位于 `tests/support/` |
| `ci/`、`.github/workflows/` | 测试执行、版本比较和工作流编排 |
| `examples/project/` | 可改用于项目指令和 `.virgo/` 配置的示例文件 |
| `docs/en/`、`docs/zh-CN/` | 一一对应的维护文档 |

安装后的命令和 `python -m virgo` 均进入 [`cli/main.py`](../../../src/virgo/cli/main.py)。根目录的小型 `main.py` 是兼容启动器。运行时代码放在包内，保证源码和安装后的 wheel 使用同一实现。

跨调用方共用的规则放在其语义归属层：`ai/providers/reasoning.py` 为请求构造和诊断元数据提供 OpenAI 推理强度映射；`harness/permissions.py` 管理权限模式、本地化显示名称和计划模式工具筛选。具体审批策略继续位于 `features/permission.py`，并兼容导出旧公开函数。功能通过 `AgentSession.runtime_for_context()` 解析可信调用方，不自行复制身份检查，也不借用当前选中的 Runtime。保留的分层与需要单独设计的接口见[抽象边界](abstraction-boundaries.md)。

## 会话与 Runtime

`AgentSession` 持有工作区路径、生效配置、认证、会话级翻译器、UI 适配器、已注册工具／命令、共享服务和诊断日志。每个 `Runtime` 持有一份 Agent 上下文：主 Agent、子 Agent 或队友。构造时显式绑定工作目录、工具、模型、对话存储、记账存储和审查归属。

候选 Runtime 在激活前构建系统上下文。`RuntimeContext` 让支持 Runtime 的提示段读取候选存储和角色，无需临时替换 `session.main` 或 `session.store`。候选创建失败时，活动对话保持原状。

全屏应用为活动对话保留各自控制器。切换选中的对话只改变显示，不会重分配后台工作。异步回调在入队时绑定原始存储和审查来源；共享记账存储不授予访问其他 Runtime 诊断的权限。

## 请求与工具流程

1. 命令或提示进入会话。命令在本地执行；提示模板和显式技能展开为模型输入。
2. Runtime 准备工具快照、待发布的 schema、上下文兼容性、压缩和诊断通知。
3. 统一消息和工具定义适配为当前模型协议，厂商流式事件进入 Agent 循环。
4. 准备并校验工具参数。Harness 先做权限预检，按顺序运行钩子，紧邻真正执行前再次检查授权。
5. 工具通常并行执行，需要串行的工具除外。结果钩子可转换结果，但执行／传输事实保留。
6. 结果和事件追加到对话。steering 在工具批次之间插入，follow-up 与已启用的停止钩子决定是否继续。

循环本身不执行应用 I/O：工具 callable 负责执行，事件订阅者负责显示。界面通过 `UI` 与 Harness 交互；全屏桥接层将工作线程更新入队，避免线程直接操作 Textual 控件。

## 需要维护的契约

**消息归属。** `ToolResult.content` 是模型可见的文本／图片，`details` 保存结构化 UI／状态元数据，不能直接整体序列化给厂商。诊断投影是显式且有界的例外：只在新的失败结果中添加安全摘要，其余证据通过授权工具读取。

**稳定请求前缀。** 在配置／schema 变化和压缩边界之间，system/tools 前缀保持稳定，新消息追加于前次请求。不要在每次模型调用时重新扫描并改写旧结果。模型、权限、Shell 和语言变化是显式配置边界；原生上下文需核对当前模型／系统提示／工具绑定。

**权限检查。** 策略提供 `preflight(event)` 和 `before_execute(event)`，不只依赖事件订阅。Harness 保持与命令和用户钩子的相对顺序，执行前检查负责捕获参数、工作区或授权变化。策略异常会阻止执行。

**持久化失败。** `SessionStore` 的逻辑记录与磁盘追加成功分离。保存错误会标记存储、通知所属 Runtime 并暂停后续调度。诊断日志独立存在，报告错误时不会递归写入已损坏的会话。界面切换或事件结束不代表持久化成功。

**输出与副作用。** 大型工具输出会限额并写入文件。失败不代表未开始执行，工具执行状态、结果钩子成功与传输完成需要分别保留；恢复时不能仅凭 `read_only` 推断幂等性。

**本地化。** 翻译器属于会话，并传入 UI／工具上下文。语言词条保存显示文本，不保存协议标识、可执行命令、用户文本或远程载荷。详见[本地化](localization.md)。

## 上下文、诊断与恢复

基础系统上下文组合身份／指导、扩展提示段、项目指令、可选自定义系统文本和实际环境。`_base_context` 在请求中加入选定的回复语言偏好，不会每轮重建基础提示。技能与记忆先展示索引，空资源不注入大段提示。配置和能力事实来自各自所属的服务。

压缩保留完整历史，只替换模型的活动上下文。受支持的官方服务使用原生表示，其他服务使用续接摘要和近期消息。上下文钩子应保持追加式行为，除非它代表明确记录的配置边界。

`diagnostic_view.py` 投影安全、有界摘要，`diagnostic_access.py` 管理逐 Runtime 的授权和分页，`recovery.py` 保存已授权 ID、待投递／已投递证据，并在正常请求边界追加通知。Provider 重试继续由 `Runtime._attempts` 负责，取证不引入另一套重试循环。

MCP 恢复在替换活动句柄前验证新连接和 schema。schema 变化在工具批次结束后的安全请求边界发布，受限 Runtime 保持原能力限制。重连不重放业务操作，状态与预算见[故障排查](../guides/troubleshooting.md)。

## 扩展入口

工具、命令、事件处理和提示段使用 [`ExtensionAPI`](../../../src/virgo/harness/extensions.py)。新模型协议使用 AI 适配器，新命令语言使用 Shell 适配器，显示语言使用语言包。策略放在 Harness／features，底层执行放在 tools／适配器。

继续阅读[扩展](extensions.md)、[本地化](localization.md)或[开发与 CI](testing-and-ci.md)。
