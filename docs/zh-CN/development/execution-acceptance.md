# 原生执行验收

运行 `uv sync --locked --dev` 安装锁定的依赖。

## 一级支持平台（Linux、macOS、原生 Windows 与 Windows + WSL2）

### Linux 与 macOS

在 Linux 或 macOS 主机上运行强制原生沙箱与策略验收套件：

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_v2.py \
  tests/harness/test_safety_v2.py \
  tests/harness/test_output_store.py \
  --junitxml=native-sandbox.xml
```

校验真实仓库规模下的冷启动（`<= 5 秒`）与单次 `Read` 开销门槛（S5-04 / VER-11）：

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets
```

### 原生 Windows（标准 AppContainer）

原生 Windows 运行非提权标准 AppContainer 后端（`windows-appcontainer`，无需管理员权限），并作为 **PR 阻塞的一级支持门槛**：
- `windows-sandbox.yml`（`Windows sandbox boundaries`）在 `pull_request`、向 `main` 推送、每日定时任务和手动触发时运行 `windows-2022` 与 `windows-2025` 矩阵，并在 `windows-2025` 上额外运行 `standard-user` 任务：创建纯 `*Users` 本地标准用户（`virgo_std`，不在 `Administrators` 组内），通过 `ci/run_as_standard_user.py` 执行 `tests/harness/test_windows_sandbox_e2e.py`（`--assert-standard-user` 校验 `IsUserAnAdmin() == False`、`TokenElevation == 0`、`TokenElevationType == 1` 且令牌组中不含 `S-1-5-32-544`），并由 `ci/check_native_report.py` 校验零跳过报告。
- `pr-platform-comparison.yml` 在 Windows 上运行完整 pytest 套件，不再跳过原生沙箱测试。
- `permission-timing.yml` 在 `windows-latest` 上针对 `no-limit`、`read-only`、`workspace-edit` 执行 `ci/measure_permission_modes.py --check-budgets`。

如需在非提权 PowerShell 会话中手动运行原生 Windows 验收（使用用户自有或 AAP 可读的原生 x64 Python 3.11+）：

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
uv run --no-sync python ci/check_native_report.py windows-sandbox.xml

uv run --no-sync pytest -q `
  tests/harness/test_policy_v2.py `
  tests/harness/test_safety_v2.py `
  tests/harness/test_output_store.py

uv run --no-sync python ci/measure_permission_modes.py `
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 `
  --realistic-files 1000 --check-budgets
```

记录任何原生 Windows 实测结果时，请一并注明操作系统构建号与账户类型（纯标准用户 vs UAC 过滤非提权桌面令牌 vs 管理员）。

### Windows + WSL2（Ubuntu）

从位于 WSL2 Linux 文件系统内（而非 `/mnt/c`）的仓库根目录执行验收脚本：

```sh
./ci/wsl2_acceptance.sh
```

一级平台须实际执行原生套件，零失败、零错误、零跳过。核对同一候选提交的 JUnit 报告、跨平台测试比较（不再使用绑定 SHA 的退役文件；删除测试须在 PR 描述中声明替代覆盖）与性能门槛。

另见：[执行边界与沙箱](../guides/sandbox.md) · [原生 Windows 沙箱](../guides/windows-sandbox.md) · [WSL2 使用指南](../guides/wsl2.md) · [权限性能验收](permission-timing.md)
