# 迁移到 Core + 插件

[文档首页](../README.md) · [配置](../reference/configuration.md) · [职责边界](../development/core-plugin-boundaries.md) · [English](../../en/guides/migrating-to-core-plugins.md)

## 有哪些变化

Virgo 默认使用 `coding` 预设。基础 Harness 保留模型请求、文件／Shell 工具、权限、上下文、会话和诊断；编程相关的可选能力通过插件提供。Schedule、Workflow、Team、持久 Tasks、通用 Memory 和 Goal 退出官方维护。

升级不会删除或改写这些功能的数据。已保存的定时任务不再执行；工作流脚本和记忆文件不再自动装载。Virgo 会提示已退役配置及残留的定时任务文件。将工作迁往外部调度器前，先检查下列数据位置。

```bash
virgo --profile coding
virgo --profile minimal --no-extensions
```

`minimal` 关闭预设中的全部能力插件；显式插件配置仍可添加需要的能力。`--no-extensions` 保留原有含义：跳过用户／项目 Python 扩展，它不是预设的别名。`minimal` 仍可加载已信任的用户／项目扩展；如上同时使用两个参数才是零扩展会话。

## 转换配置

```json
{
  "profile": "coding",
  "plugins": {
    "web": {"enabled": true},
    "mcp": {"enabled": false},
    "todo": {"enabled": true},
    "auto_review": {"enabled": true}
  }
}
```

只接受已知插件 ID。默认值、用户、项目、运行时／CLI 配置依次合并。每一层先转换旧 `features` 设置，再执行合并；同一层的新 `plugins` 设置覆盖对应旧字段。更高优先级的项目配置仍覆盖用户配置，即使项目使用旧写法。转换只给出提示，不改写配置文件。

| 旧配置 | 新配置／行为 |
| --- | --- |
| `features.skills`、`todo`、`ask_user`、`subagent`、`background`、`mcp` | `plugins.<id>.enabled` |
| `features.diagnostics` | `plugins.diagnostic_get.enabled`；Core 诊断仍保留 |
| `features.search`、`features.fetch` | `plugins.web.searchEnabled`、`plugins.web.fetchEnabled`；由 `web` 插件组合提供 |
| `features.permission=false` | 提示后转换为 `plugins.auto_review.enabled=false`；Core 权限策略始终装配 |
| 已移除功能开关及 `memory`、`workflow`、`goal` 配置 | 只提示，不执行后台工作或存储迁移 |

如需有意识地跳过权限审查，使用既有的显式 `permissions.mode=accept-all`；禁用 Feature 不再是绕过权限的方法。禁用 `auto_review` 后，工作区规则和执行期检查仍在；不确定操作需要人工批准，无人审批时拒绝执行。

`search.maxUses` 和 `fetch.maxUses` 从默认三次改为 `null`；需要时可显式设置正整数上限。`plugins.web.maxCalls` 可限制每个调用运行时的一次用户请求中客户端搜索与读取的总调用数；单次操作的时间和输出限制保留。详见[联网能力](web.md)。

## 信任项目扩展

项目 Python 扩展可以用户身份执行任意代码。Core 工具权限门和插件错误隔离都不是 Python 沙箱。对陌生仓库授权前，应先审查 `.virgo/extensions/*.py`。

使用 `--trust-project-extensions` 为本次会话信任所选项目。如需持久授权，将项目规范化的绝对目录写入**用户配置**的 `extensions.trustedProjects`：

```json
{
  "extensions": {
    "trustedProjects": ["/absolute/path/to/reviewed-project"]
  }
}
```

项目自己的配置不能为自己的扩展授权。用户级 `<用户>/extensions/` 原本就在用户控制下。即使已授权，`--no-extensions` 仍会禁用这两个位置。不要把个人信任清单或凭据提交到项目。

## 只读检查历史数据

`<用户>` 指 `$VIRGO_HOME` 或 `~/.virgo`；`<项目>` 指工作目录；会话 JSONL 文件位于 `<用户>/sessions/<工作区标识>/`。

| 退役能力 | 保留的路径／记录 | 人工替代方式 |
| --- | --- | --- |
| Schedule | `<项目>/.virgo/schedules.json` | 检查提示、时区和规则后，再配置 cron、系统调度器或 CI |
| Workflow | `<用户>/workflows/*.py`、`<项目>/.virgo/workflows/*.py`；`<项目>/.virgo/runs/*.jsonl` | 将审查后的编排迁为项目脚本或 CI；旧 Agent 成功日志不代表任意 Python 副作用都可安全重放 |
| Tasks | `<项目>/.virgo/tasks/*.json` 及锁文件 | 将需要保留的记录导出到 Issue 系统或项目文档 |
| Memory | `<用户>/memory/`、`<项目>/.virgo/memory/` 及索引 | 人工挑选与项目有关的知识，放入 `AGENTS.md`、`docs/` 或 Skills |
| Team | `<项目>/.virgo/worktrees/`；`<会话目录>/<lead-id>-team/*.jsonl` | 改用聚焦的子 Agent；先通过 Git 检查旧工作树，再决定保留或删除 |
| Goal | 会话 JSONL 中 `kind=goal` 的 `custom` 条目及辅助调用记录 | 将完成条件写入任务提示或项目验收说明 |

把 JSON／JSONL／Markdown 作为普通文件阅读，或复制到导出目录即可。没有迁移命令会执行 Python 工作流、激活定时任务、删除数据或上传内容。记忆可能包含个人信息，写入 Git 前需人工筛选。升级前停止旧版 Virgo 进程，可避免旧进程继续运行其调度器。

## 旧会话和已移除命令

解析会话不要求历史工具仍然安装。旧工具结果仍可展示并进入上下文；新调用不存在的工具时会明确返回不可用。历史记录不会自动重放操作。

`/schedule`、`/schedules`、`/workflow`、`/team`、`/tasks`、`/memory` 和 `/goal` 不再提供原能力。插件开启时，`/todos`、`/jobs`、Skills 和 `agent` 仍可使用。未完成的 Todo 不强制续跑，Agent 的正常停止行为保留。

## 扩展作者

官方实现迁到 `virgo.plugins`；集成时使用文档化的 `ExtensionAPI`。保留能力的 `virgo.features` 导入路径仅在 0.2 系列内提供兼容转发，升级 0.3 前应迁移。已退出维护的子系统不保留兼容实现。

工具和命令重名现在明确失败，不再覆盖已有注册。用 `api.on_cleanup` 注册清理，用 `api.replace_tools` 发布动态工具集。异步任务捕获实际调用方的 Runtime／Store，子运行时通过工厂创建，不直接修改 `session._tools`、`session._lock` 或当前 UI 状态。公开接口见[扩展指南](../development/extensions.md)。

## 回滚

本次不做破坏性存储迁移。回滚时先停止新版进程，再返回旧 Git 修订或安装版本；启动旧版前检查定时任务，因为旧代码可能恢复其调度器。保留现有会话与数据文件，无需自动 reset、clean、迁移重放或删除。
