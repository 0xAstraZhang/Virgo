# 原生执行与性能验收

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/development/execution-acceptance.md)

运行 `uv sync --locked --dev` 安装锁定的依赖。每个一级支持平台（Linux、macOS、原生 Windows 与 Windows + WSL2）均须针对同一候选提交执行原生隔离套件（零失败、零错误、零跳过）并通过真实规模性能预算。

## 原生隔离验收套件

### Linux 与 macOS

在真实的 Linux 或 macOS 主机上运行强制原生沙箱套件并校验其 JUnit 报告（与 `.github/workflows/native-sandbox.yml` 一致），再单独运行跨平台策略套件：

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  --junitxml=native-sandbox.xml
uv run --no-sync python ci/check_native_report.py native-sandbox.xml

uv run --no-sync pytest -q \
  tests/harness/test_policy_boundaries.py \
  tests/harness/test_guardrails.py \
  tests/harness/test_output_store.py
```

### 原生 Windows（标准 AppContainer）

原生 Windows 在已更新的新版 Windows 11（`null.sys 10.0.26100.8972+` / WIL Feature `60692039`，`\Device\Null` 原生授予 `S-1-15-2-1` `0x001201BF`）上运行非提权标准 AppContainer 后端（`windows-appcontainer`，无需管理员权限），并作为 **PR 阻塞的一级支持门槛**：

- GitHub Actions 托管的 Windows Server 2022/2025 runner 默认搭载未启用 Feature `60692039` 的旧版 `null.sys`。Virgo 生产代码**绝不**修改 `\Device\Null`（不受支持的主机默认以 **No-Limit** 启动并给出提示，显式请求沙箱模式时 fail-closed）。
- `windows-sandbox.yml`（`Windows sandbox boundaries`）在 `pull_request`、向 `main` 推送、每日定时任务和手动触发时运行 `windows-2022`（原生）以及 `windows-2025` 上的 `standard-user` 任务。两个 runner 均先运行 `ci/prepare_windows_null_device.py --verify-unsupported` 验证原生旧 Server 的默认降级与 fail-closed 行为，再通过 `ci/prepare_windows_null_device.py` 对齐 CI runner 的 `\Device\Null` DACL，随后端到端运行完整标准 AppContainer 隔离套件（含 `NUL`/`subprocess.DEVNULL` 与纯 `*Users` 非管理员账号 `virgo_std` 的 `ci/run_as_standard_user.py --assert-standard-user` 验收），并由 `ci/check_native_report.py` 校验零跳过报告。
- `pr-platform-comparison.yml` 针对单一候选提交（`head`）在 `ubuntu-latest`、`windows-latest`（`windows-2025`，通过 `ci/prepare_windows_null_device.py` 对齐 `\Device\Null` 且不跳过原生沙箱测试）与 `macos-latest` 上运行完整 pytest 套件并强制三平台全绿。 部分重跑时，汇总选取每个平台最新一次执行的报告。pytest 失败的任务上传报告后仍会标记为失败，可通过 GitHub 的“Re-run failed jobs”再次运行。

如需在非提权 PowerShell 会话中手动运行原生 Windows 验收（使用用户自有或 AAP 可读的原生 x64 Python 3.11+）：

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
uv run --no-sync python ci/check_native_report.py windows-sandbox.xml

uv run --no-sync pytest -q `
  tests/harness/test_policy_boundaries.py `
  tests/harness/test_guardrails.py `
  tests/harness/test_output_store.py
```

记录任何原生 Windows 实测结果时，请一并注明操作系统构建号与账户类型（纯标准用户 vs UAC 过滤非提权桌面令牌 vs 管理员）。

### Windows + WSL2（Ubuntu）

从位于 WSL2 Linux 文件系统内（而非 `/mnt/c`）的仓库根目录执行验收脚本：

```sh
./ci/wsl2_acceptance.sh
```

脚本会检查 `bubblewrap`、运行带 `VIRGO_REQUIRE_SANDBOX_TESTS=1` 的 Linux 原生沙箱测试、通过 `ci/check_native_report.py` 校验 JUnit 报告，并校验 **Workspace-Edit** 性能预算。

## 性能预算

`Permission mode performance` 工作流（`.github/workflows/permission-timing.yml`）在 Linux（`ubuntu-latest`）、macOS（`macos-latest`）和原生 Windows（`windows-latest`，先通过 `ci/prepare_windows_null_device.py` 对齐 `\Device\Null`）上覆盖 `no-limit`、`read-only` 与 `workspace-edit` 三种模式。

在全部三个平台上，`--check-budgets` 使用本地模拟模型驱动 Virgo 真实工具（零外部网络调用、零审批弹窗），对比小工作区（`0` 个额外文件）与真实规模工作区（`1000` 个文件）的耗时预算：

- **冷启动与首次 Shell 命令**：`<= 5 秒`
- **连续短 Shell 命令**：`<= 2 秒` 且随工作区文件规模保持有界 O(1) 开销
- **Broker `read` 延迟**：`<= 250 毫秒` 且随工作区文件规模保持 O(1) 开销

每个样本都在全新的 Python 解释器、独立用户目录与工作区中运行，记录导入耗时、会话初始化耗时、`read` 耗时、首次 Shell 命令耗时、连续短命令耗时及编辑到测试总耗时。失败样本会保留诊断信息；若子进程清理未确认，会立即中止后续采样。

在本地运行同一预算检查：

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets --output budget.json
```

原生进程与网络隔离边界由专门的原生沙箱验收工作流与脚本（`native-sandbox.yml`、`windows-sandbox.yml`、`ci/wsl2_acceptance.sh`）独立验证。重构期间删除测试时，须在提交信息与 PR 描述中列出每项被删除测试及其替代覆盖。

另见：[执行边界与沙箱](../guides/sandbox.md) · [原生 Windows 沙箱](../guides/windows-sandbox.md) · [WSL2 使用指南](../guides/wsl2.md) · [执行接口](execution-interface.md)
