# 权限模式耗时

`Permission mode performance` 工作流在 Linux、macOS 和 Windows 上分别对比当前 PR 提交和固定的主线提交 [`04e94ed`](https://github.com/0xAstraZhang/Virgo/commit/04e94ed45992d8e2cbf2178c8c6b562704092a83)。两个版本使用各自锁定的依赖，依次在同一台运行器上执行。脚本用本地模型驱动 Virgo 的实际工具。

| 模式 | 主线对照配置 | 源文件修改者 |
| --- | --- | --- |
| No-Limit | 原版 No-Limit 预设 | Virgo 文件工具 |
| Workspace-Edit | 原版 Workspace-Edit 预设 | Virgo 文件工具 |
| Read-Only | 原版 Read-Only 预设，显式关闭网络 | 外部编辑器 |

每个版本运行三个样本，各自使用新的解释器、项目和最初尚不存在的用户目录。项目的 `AGENTS.md` 包含一个标记，只有会话构造后的系统提示词实际包含该标记，样本才继续执行。自动上下文读取被拒绝或省略时，不能计作成功的冷启动。原始样本以 `project_context_loaded` 记录检查结果，比较时要求两个版本均通过。

报告给出冷启动、第一条 Shell 命令、连续五条短命令，以及从编辑源文件到测试读到新值的总耗时四项中位数。每个成功样本须为零审批。脚本保留失败样本的诊断，无法确认清理时停止后续采样。比较前核对提交、已跟踪文件的差异、脚本摘要、平台、Python 版本和工作负载。门禁验证可比较的工作确实成功完成，不设置延迟阈值，也不代表没有性能回退。

在被测版本安装锁定环境 `uv sync --locked --dev` 后运行工作负载。

```sh
uv run --no-sync python ci/measure_permission_modes.py --mode workspace-edit --samples 3 --repeat 5 --output timing.json
```

测量固定主线时，使用同一份 PR 脚本并传入 `--contract original`。Read-Only 任务还会使用 PR 版本的网页工具读取固定的 HTTPS 文件，核对内容摘要，并确认 Shell 网络策略处于关闭状态。实际的 Shell socket 拒绝由专门的验收工作流验证。
