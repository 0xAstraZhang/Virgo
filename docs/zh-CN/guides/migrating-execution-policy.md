# 执行配置迁移

新配置默认 Host，但旧配置明确提出的隔离要求不会静默降级。仅在确实需要宿主权限时删除旧要求；否则显式选择 Sandbox，并阅读[当前阻塞](../development/execution-acceptance.md)。

| 旧接口／配置 | 迁移 |
| --- | --- |
| `sandbox.mode`、`--sandbox` | 明确选择 `execution.provider`／`--execution`；Sandbox 需用户安装并配置 MXC。 |
| `readOnly/workspaceOnly` | 工作区只读映射为 `readPaths:["."],writePaths:[]`；工作区可写映射为两者 `["."]`。单独核对网络权限，不自动扩大范围。 |
| `no-limit` | 选择 Host，不再作为 Sandbox 预设。 |
| 非空 `permissions.allow/deny` | 移除命令规则。操作约定写入提示，技术保证用资源能力表达；“禁止 git push”不能等价映射为路径列表。 |
| `permissions.mode/modes` | `preset/presets` 的过渡拼写，新路径 schema 和显式 provider 要求仍有效。 |
| 项目中的执行／权限配置 | 权限和扩展信任移至用户配置或显式启动参数。 |
| `/mode`、`/sandbox` | 使用 `/permissions show`；只有 Sandbox 提供资源预设和增量请求策略。 |
| Agent／Runtime／ExtensionAPI 的 `read_only` | 删除参数；子代理继承来源上下文。 |
| `services.permissions/services.sandbox`、`PermissionPolicy/register_permission_policy` | 改用单一所有者执行 provider。可信 Python 本身在受管边界之外。 |
| `sandbox_policy` | 传递已捕获的 `execution_context`，不能从后来可见会话取权限。 |
| `details.sandbox/command_started` | 读取 `details.boundary/payload_started`；`details.execution.started` 只代表进入 Python 工具函数。 |

旧原生后端、命令解析器、普通审批和会话审批缓存已移除。ask/never 只控制 Sandbox 资源增量。历史授权不能恢复执行能力。Windows 旧 LPAC 后端已移除，MXC 替代仍未通过；macOS 不再提供 coalition 监督保证。
