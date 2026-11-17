# 执行接口与资源预设

Core 通过 `virgo.execution` 的中立接口访问文件和启动进程。Host 内置；显式 Sandbox 在 Runtime 构造前加载必要扩展并绑定单一所有者，加载失败终止执行。普通可信扩展的可选加载机制不能提供 Host 回退。

Runtime 捕获不可变执行上下文，包括来源工作区、provider、资源策略与版本。文件编辑匹配和输出业务逻辑保留在 Core，底层文件保护、MXC 适配及资源授权属于扩展。进程句柄统一支持等待、取消和清理。

预设使用 name、readPaths、writePaths、networkAllowed；可写同时可读。`ask|never` 仅控制资源增量申请。来源、完整参数、环境、策略及资源身份共同绑定一次授权，最终校验后原子消费，五分钟或 run 结束失效，不能由历史恢复。

配置例子、迁移和信任范围见[配置参考](../reference/configuration.md)、[迁移](../guides/migrating-execution-policy.md)。原生共享路径策略当前拒绝执行，不能将文件工具保护当作 Shell 隔离，见[验收状态](execution-acceptance.md)。
