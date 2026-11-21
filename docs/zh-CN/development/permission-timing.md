# 权限模式耗时

`Permission mode performance` 工作流在 Linux（`ubuntu-latest`）、macOS（`macos-latest`）和原生 Windows（`windows-latest`）上覆盖 `no-limit`、`read-only` 与 `workspace-edit` 三种模式。在**全部三个平台**上，工作流通过 `--check-budgets` 强制校验真实规模（`0` 对比 `1000` 文件）下的冷启动与首次命令初始化（`<= 5 秒`）、重复命令 O(1) 开销（`<= 2 秒`且不随文件数线性增长）以及 `Read`（`<= 250 毫秒`且 O(1)）预算。脚本使用本地模拟模型驱动 Virgo 的真实工具，不依赖外部网络。

每个样本都在全新的 Python 解释器、独立用户目录与工作区中运行，要求零审批弹窗，并记录导入耗时、会话初始化耗时、`Read` 耗时、首次 Shell 命令耗时、连续短命令耗时及编辑到测试总耗时。失败样本会保留诊断信息；若子进程清理未确认，会立即中止后续采样。

在完成 `uv sync --locked --dev` 后，可在本地运行同一预算检查：

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets --output budget.json
```

原生进程与网络隔离边界由专门的原生沙箱验收工作流与脚本（`native-sandbox.yml`、`windows-sandbox.yml`、`ci/wsl2_acceptance.sh`）独立验证。
