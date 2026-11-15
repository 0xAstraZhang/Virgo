# 原生命令沙箱

[配置参考](../reference/configuration.md) · [English](../../en/guides/sandbox.md)

## 启用与查看

原生隔离需要显式启用。默认 `disabled` 保持现有执行方式，不代表具有操作系统保护。启动时选择边界：

```bash
uv run virgo --sandbox workspace-write
uv run virgo --sandbox read-only
```

用户配置（`$VIRGO_HOME/settings.json`，默认 `~/.virgo/settings.json`）支持：

```json
{"sandbox": {"mode": "workspace-write", "network": "restricted"}}
```

项目中的 `sandbox` 配置会被忽略并提示。显式启动覆盖值优先于用户配置。未知模式、字段和网络值会导致配置失败。`/sandbox` 显示请求的模式与探测到的启动器，不改变权限，也不证明当前内核允许隔离。每次启动都会先探测实际执行边界，再启动业务命令。

## 执行边界

`workspace-write` 允许写入来源工作区，系统及解释器目录只读；`read-only` 将工作区也设为只读。每个命令有独立的可写临时目录和私有 `HOME`。宿主保留该命令输出日志的精确描述符。子进程只继承 `PATH`、语言／时区／终端变量，以及私有 home/temp 路径和 `VIRGO=1`；厂商密钥及其他环境凭据不会转发。

当前唯一支持的活动网络策略是 `restricted`。Linux 使用独立网络命名空间，并用 seccomp 禁止 socket（包括按文件路径访问的 Unix socket）和 io_uring。macOS 使用默认拒绝、没有网络授权的 Seatbelt 配置。关闭隔离必须显式指定启动配置，审批结果或自动重试不能关闭沙箱。

前台 Shell、模型请求的后台命令、子运行时 Shell 共用启动器。工作区取自来源 Runtime，而不是当前可见对话或子任务更换后的工作目录。Plan 与只读子任务会把新启动命令收窄为只读。已经运行的进程保留启动时边界；模式变化不会追溯撤回其权限。

`accept-all`、allow 规则、模型审查和人工批准都不会关闭已启用的沙箱。缺少启动器／系统库、不支持的平台、命名空间拒绝或 Seatbelt 配置失败返回 `sandbox_unavailable`，不在宿主直接执行业务命令。普通非零退出仍是普通命令失败，不猜测为沙箱拒绝，也不会自动重放可能已产生部分副作用的操作。Shell 结果记录实际模式与工作区，后台任务结果另外保留后端信息。

## 平台要求与限制

Linux 需要 root 所有的系统 `bwrap`（支持 `--disable-userns`）、系统 `libseccomp.so.2` 和创建相应命名空间的权限。当前 seccomp 实现支持 x86-64 与 AArch64。macOS 需要系统 `/usr/bin/sandbox-exec`。本阶段没有 Windows 原生后端：启用沙箱的请求会拒绝执行；`disabled` 保持现有 Windows 执行器。

允许读取的范围包括实际存在的系统目录（`/usr`、`/bin`、`/sbin`、`/lib`、`/lib64`、`/etc`、`/opt`；macOS 另含 `/System/Library`、`/System/Cryptexes`、`/System/Volumes/Preboot/Cryptexes/OS`、`/Library`、`/dev`、`/private/etc`）、解释器安装目录、工作区与独立临时目录，不会挂载整个宿主用户目录。位于范围外的工具、依赖缓存、证书或 SDK 可能无法使用；暂不支持额外挂载和网络例外。若工作区包含 Virgo 用户凭据／会话目录或沙箱／解释器安装目录，会拒绝执行，不暴露可写的可信运行环境。

工作区根部已经存在的 `.git`、`.virgo`、`AGENTS.md` 和 `CLAUDE.md` 不允许由 Shell 写入。Linux 使用只读挂载保护已存在路径，不能阻止创建尚不存在的这些名称；macOS 也排除尚不存在的名称。Core 的 `write`／`edit` 工具另行在应用层执行工作区、只读和元数据限制，这些 Python 文件操作不处于 Shell 的 OS 沙箱内。

这不是宿主 Harness、可信 Python 扩展、项目 hooks、远程 MCP 服务、厂商托管工具或可信集成直接调用底层执行函数的沙箱。可写工作区仍允许破坏普通项目文件。本阶段不提供资源配额、读取授权管理、单命令提权、Windows 隔离或运行中任务的权限撤销。

## 实现与验证

`harness/sandbox.py` 绑定会话策略和调用方，并检查文件工具上限；`tools/sandbox.py` 构造及探测原生启动边界。进程生命周期继续由现有 Shell 执行、输出记录、取消及后台任务机制维护，没有新增 Agent Loop。

`tests/harness/test_sandbox_e2e.py` 覆盖真实装配会话和进程：后端不可用、配置来源、文件工具限制、间接及符号链接写入、子任务／后台归属、网络／环境隔离、已有元数据、退出码与超时。原生 CI 设置 `VIRGO_REQUIRE_SANDBOX_TESTS=1`，将后端不可用视为失败，而不是能力跳过。仅通过模拟失败场景不能证明 OS 隔离有效。
