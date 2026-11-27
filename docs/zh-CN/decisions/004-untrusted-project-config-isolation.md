# 004 — 不可信项目配置隔离与授权指纹绑定

[决策索引](README.md) · [文档中心](../README.md) · [English](../../en/decisions/004-untrusted-project-config-isolation.md)

**状态：** 已落地

## 背景

在任意仓库中打开 Virgo 时，仓库内自带的配置文件绝不能在未经用户确认的情况下执行宿主代码、降低沙箱模式、劫持用户 API 端点、借由配置合并清空用户级 hooks/MCP 服务器，或在 MCP 服务器定义变化后继续复用旧的**始终允许**授权。

## 决策

- **执行权限归用户独占**：项目 `.virgo/settings.json` 绝对禁止配置 `sandbox`、`execution` 或 `permissions`（`virgo.harness.settings` 中的 `Settings.load()` 直接报错），项目配置和运行时覆盖项也无权写入 `extensions.trustedProjects` 或安装持久化授权。
- **合并前剥离不可信活跃配置**：对未通过用户级 `extensions.trustedProjects` 或 `--trust-project-extensions` 显式信任的工作区，`Settings.load()` 会在 `deep_merge` **之前**剥离 `hooks`、`mcpServers`、`providers`、`extensions`、`shell`、`search` 和 `fetch`，扩展和 MCP 加载器也会跳过项目的 `.virgo/extensions/*.py` 与 `.virgo/mcp.json`。在 TUI 中切换工作区时，`trust_project_extensions` 会重置为 `False`。
- **拒绝符号链接项目提示词文件**：`virgo.harness.prompt` 拒绝读取符号链接形式的项目 `AGENTS.md`、`CLAUDE.md`、`SYSTEM.md` 或 `APPEND_SYSTEM.md`，防止仓库内链接将外部文件引入模型上下文。
- **MCP 授权绑定配置指纹**：只读 MCP 工具直接执行，可能修改状态的 MCP 工具在 **Read-Only** 与 **Workspace-Edit** 下需审批。**始终允许**按 `f"{tool_name}:{config_fingerprint}"` 持久化，其中 SHA-256 指纹覆盖展开后的 `command`/`args`/`env`（stdio），或规范化传输类型、含查询字符串的完整 URL 及按小写键名排序的已展开 `headers`（HTTP/SSE）；任一输入变更即令旧授权失效。
- **同轮拒绝生效**：用户明确选择拒绝（`n`）后，同一用户轮次内的重复申请或被已拒绝范围涵盖的申请直接返回 `already_denied_this_turn`，避免反复弹窗，同时不阻断无关同级路径或在写权限被拒后申请读权限。

## 取舍与约束

- **项目级自动化与路由需显式信任**：上述项目设置和文件都需要信任，包括 `shell.executable`、`search` 与 `fetch` 的后端及其 `apiKeyEnv` 设置。`plugins.web.enabled` 等普通项目选项仍遵循常规的设置优先级。
- **防止合并覆写漏洞**：未来凡是可能触发代码执行、重定向网络端点或削弱用户保护的新配置项，都必须加入 `UNTRUSTED_PROJECT_KEYS`（或在项目级直接禁止），且在合并前剥离。
- **MCP HTTP 调用不自动跟随重定向重放**：URL 型 MCP 工具调用遇到 HTTP 重定向直接失败，不向其他源重新提交可能带有副作用的请求。

## 相关文档

- [内置扩展（Todos 与 MCP）](../guides/extensions.md)
- [权限模式与审批](../guides/permissions.md)
- [架构概览](../development/architecture.md)
