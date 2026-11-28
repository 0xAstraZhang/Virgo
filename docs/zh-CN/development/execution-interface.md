# 执行接口

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/development/execution-interface.md)

执行层是分层组织的核心基础设施，由基础模块 `virgo._win32`、`virgo.execution_policy`、`virgo.execution_types`、`virgo.process`，彼此独立的执行叶子（`virgo.file_access`、`virgo.output_store`、`virgo.sandbox`）以及统一装配门面 `virgo.execution` 组成。会话负责选择 **Read-Only**、**Workspace-Edit** 或 **No-Limit**（`execution.mode`），并为发起任务捕获不可变的执行上下文（`HostContext` 或 `SandboxContext`）。子任务与延迟任务沿用该捕获上下文，而非读取当前活跃的 UI 会话状态。

## 策略与进程启动

- `virgo.execution_policy.PermissionPolicy` 是 Broker 文件工具与原生 Shell 沙箱共用的单一 deny-first 策略数据模型。核心策略定义（`ExecutionMode`、`DEFAULT_MODES`、`HOST_MODE`、`PermissionSnapshot`、`PROTECTED_NAMES = (".virgo",)`、`PRIVATE_HOME_RELATIVES`、`default_private_paths`、`default_deny_rules`、`filter_sandbox_environment`）作为纯叶子模块不导入 `virgo.sandbox`、`virgo.execution` 或 `virgo.harness`；平台默认模式探测（`is_windows_sandbox_supported()`、`default_execution_mode()`）与 `HostContext`/`SandboxContext`/`SandboxExecutor` 装配位于 `virgo.execution`。针对私有凭据路径的硬性读拒绝不可覆盖（`hard_denied`），而针对项目 `.virgo` 的可确认写拒绝可在用户明确授权后通过 `policy.grant(...)` 放行。
- `virgo.execution_types` 定义共享执行契约（`ExecutionContext`、`ProcessRequest`、`ProcessHandle`、`ExecutionUnavailable`、`ExecutionFailed`、`ExecutionCancelled`、`FileAccessDenied` 及环境辅助函数），`virgo.process` 提供 POSIX `HostProcess` 与 Windows `WindowsProcess` 托管原语，避免与 `virgo.execution` 或 `virgo.tools` 产生循环依赖。
- `SandboxExecutor.run_process(request, context, cancellation)` 是本地进程的统一启动入口。`ProcessRequest` 显式声明 `argv`、`cwd`、基础 `env`、经过校验的可选 `env_overrides` 与标准流。无限制上下文（包括 No-Limit Shell 与通过 `HostContext.unrestricted` 启动的 Host stdio MCP 服务器）直接在宿主执行，不探测原生沙箱后端。
- 原生 Shell 适配器将 `PermissionPolicy` 编译到各平台后端：
  - **Linux（`bubblewrap`）**：`--ro-bind / /` 宽只读宿主视图、遮蔽 `private_paths`、丢弃 Linux capabilities、`--info-fd` 启动确认，且不依赖 `libseccomp`。
  - **macOS（Seatbelt）**：直接使用 `/usr/bin/sandbox-exec`（每个执行器仅探测一次）配合 `HostProcess` 进程组托管。
  - **原生 Windows（标准 AppContainer `windows-appcontainer`）**：支持 NTFS 与 ReFS Dev Drive、会话级单 SID 与单次 OS 传播的 `SetNamedSecurityInfoW` 可继承 ACE、自动为 `.venv` 底层 Python 与用户自有 `PATH` 工具链授予只读 ACE、对未获批 `.virgo` 调用 `protect_readonly_path()`、在祖先目录缺少 `S-1-15-2-1` 时映射会话内 `DefineDosDeviceW` 虚拟盘符、预写 JSON 崩溃恢复日志，以及直接 `CreateProcessW` + `WindowsProcess` 关闭即终止的 Job Object 托管。
- 启动确认、取消、进程树清理与禁止自动重放诊断均由这些后端实现负责。不可用的原生边界绝不会静默回退到 Host（交互式 Shell 会话可在载荷启动前向用户请求一次性的显式 No-Limit 回退）。载荷释放后的任何失败（`ExecutionFailed`）绝不会触发自动重放。

- 在 macOS 上有可用 Shell 时，面向模型的环境提示会说明系统命令使用 BSD 参数，并建议用 `od` 或 Python 检查空白和换行。

## Broker 文件工具、OutputStore 与 Host MCP

- **文件工具**（`virgo.tools.fs` + `virgo.file_access`）直接在 Broker 进程内按 `PermissionPolicy` 强制执行权限校验，配合末级组件 `O_NOFOLLOW` 与临时文件原子 `os.replace`，不引入独立的 OS 沙箱辅助进程（见[决策 002](../decisions/002-execution-boundaries-and-broker-tools.md)）。`read` 为文本行附加从 1 开始的 `<行号>|` 前缀；`glob` 与 `grep` 默认通过受策略约束的纯 Python 匹配器遵守 `.gitignore`（可用 `include_ignored=true` 关闭），`grep` 支持 `content`/`files`/`count` 模式及 `fixed_strings` 字面量搜索。
- **命令输出**（`virgo.output_store.OutputStore`）通过管道回传 Broker，按会话持久化在 `<VIRGO_HOME>/outputs/session-<id>` 下，使用紧凑的 16 字符 ID（`out_<12-hex>`，兼容旧版 48 位十六进制 ID），通过 `read_output` 提供模式搜索、尾部读取、按行分页与 UTF-8 字符边界对齐的字节分页，支持会话回收清理与 7 天 TTL。未截断的前台命令会立即删除临时落盘文件。
- **MCP 服务器**在主会话与子 Agent 间共享连接。Virgo 通过 `HostContext.unrestricted` 和 `SandboxExecutor` 启动 stdio 服务器；URL 端点由 Host 客户端连接。两类端点均执行项目信任校验与配置指纹绑定的工具授权（见 [MCP 配置](../guides/extensions.md#mcpmcp)）。
- 每个 POSIX Host 命令拥有独立的进程组（`HostProcess`）；Windows Host 命令运行在关闭即终止的 Job Object（`WindowsProcess`）中。

另见：[架构概览](architecture.md) · [权限模式与审批](../guides/permissions.md) · [原生执行与性能验收](execution-acceptance.md) · [决策 002](../decisions/002-execution-boundaries-and-broker-tools.md)
