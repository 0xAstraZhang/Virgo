# 执行术语

| 术语 | 含义 |
| --- | --- |
| execution provider | host 或显式选择的 sandbox；execution.provider／--execution |
| 资源预设 | 稳定 ID、可编辑名称、readPaths／writePaths／networkAllowed |
| 权限增量 | 一个精确操作需要的额外资源，不能切换 provider |
| 审批策略 | ask／never，只控制 Sandbox 增量申请 |
| 执行上下文 | 不可变的来源工作区、provider、资源与策略版本 |
| 请求边界 | 用户选择的策略 |
| 实际边界 | 真正实施的范围、建立状态及证据 |
| 插件 | 通过 plugins.<id>.enabled 选择的可选能力 |

新配置默认 Host，/permissions show 显示执行状态。Sandbox 管理预设和单次授权；子代理继承来源上下文。任务提示表达意图，不证明隔离。参见[迁移](../guides/migrating-execution-policy.md)和[验收](execution-acceptance.md)。
