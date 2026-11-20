# 权限模式耗时

`Permission mode performance` 工作流在 Linux（`ubuntu-latest`）、macOS（`macos-latest`）和原生 Windows（`windows-latest`）上覆盖 `no-limit`、`read-only` 与 `workspace-edit` 三种模式。在 Linux 与 macOS 上，工作流依次在同一台运行器上对比当前 PR 提交和固定的主线提交 [`04e94ed`](https://github.com/0xAstraZhang/Virgo/commit/04e94ed45992d8e2cbf2178c8c6b562704092a83)；在原生 Windows 上则直接测量当前 PR 提交（跳过旧版 LPAC 主线基线对比）。在**全部三个平台**（含 Windows）上，工作流均通过 `--check-budgets` 强制校验真实规模下的冷启动（`<= 5 秒`）与 `Read`（`<= 250 毫秒`）预算。脚本用本地模型驱动 Virgo 的实际工具。

| 模式 | 主线对照配置 | 源文件修改者 |
| --- | --- | --- |
| No-Limit | 原版 No-Limit 预设 | Virgo 文件工具 |
| Workspace-Edit | 原版 Workspace-Edit 预设 | Virgo 文件工具 |
| Read-Only | 原版 Read-Only 预设，显式关闭网络 | 外部编辑器 |

每个版本运行三个样本，各自使用新的解释器、用户目录和项目。报告给出冷启动、第一条 Shell 命令、连续五条短命令，以及从编辑源文件到测试读到新值的总耗时四项中位数。每个成功样本须为零审批。脚本保留失败样本的诊断，无法确认清理时停止后续采样。比较前核对提交、已跟踪文件的差异、脚本摘要、平台、Python 版本和工作负载。

在被测版本安装锁定环境 `uv sync --locked --dev` 后运行工作负载。

```sh
uv run --no-sync python ci/measure_permission_modes.py --mode workspace-edit --samples 3 --repeat 5 --output timing.json
```

测量固定主线时，使用同一份 PR 脚本并传入 `--contract original`。Read-Only 任务还会使用 PR 版本的网页工具读取固定的 HTTPS 文件，核对内容摘要，并确认 Shell 网络策略处于关闭状态。实际的 Shell socket 拒绝由专门的验收工作流验证。
