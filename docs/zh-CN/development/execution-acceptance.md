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

原生 Windows 在已更新的新版 Windows 11（`null.sys 10.0.26100.8972+` / WIL Feature `60692039`，`\Device\Null` 原生授予 `S-1-15-2-1` `0x001201BF`）上运行非提权标准 AppContainer 后端（`windows-appcontainer`，无需管理员权限），并作为 **PR 阻塞的一级支持门槛**：
- GitHub Actions 托管的 Windows Server 2022/2025 runner 默认搭载未启用 Feature `60692039` 的旧版 `null.sys`。Virgo 生产代码**绝不**修改 `\Device\Null`（不受支持的 Windows 默认以 **No-Limit** 启动并提示 WSL2 / No-Limit / 更新 Windows，显式请求沙箱模式时 fail-closed）。
- `windows-sandbox.yml`（`Windows sandbox boundaries`）在 `pull_request`、向 `main` 推送、每日定时任务和手动触发时运行 `windows-2022`（原生）以及 `windows-2025` 上的 `standard-user` 任务。两个 runner 均先运行 `ci/prepare_windows_null_device.py --verify-unsupported` 实证原生旧 Server 的默认降级与 fail-closed 行为，再用 `ci/prepare_windows_null_device.py` 将 CI Server runner 的 `\Device\Null` DACL 对齐到微软 `null.sys 10.0.26100.9818` 的同款 ACE，随后端到端运行完整标准 AppContainer 隔离套件（含 `NUL`/`subprocess.DEVNULL` 与纯 `*Users` 非管理员账号 `virgo_std` 的 `ci/run_as_standard_user.py --assert-standard-user` 验收），并由 `ci/check_native_report.py` 校验零跳过报告。
- `pr-platform-comparison.yml` 针对单一候选提交（`head`）在 `ubuntu-latest`、`windows-latest`（`windows-2025`，对齐 Server runner 的 `\Device\Null` 且不跳过原生沙箱测试）与 `macos-latest` 上运行完整 pytest 套件并强制三平台全绿。
- `permission-timing.yml` 在 `windows-latest` 上对齐 `\Device\Null` 后，针对 `no-limit`、`read-only`、`workspace-edit` 执行 `ci/measure_permission_modes.py --check-budgets`。

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

一级平台须实际执行原生套件，零失败、零错误、零跳过。核对同一候选提交的 JUnit 报告、跨平台 CI 汇总（不再使用绑定 SHA 的退役文件；删除测试须在 PR 描述中声明替代覆盖）与性能门槛。

另见：[执行边界与沙箱](../guides/sandbox.md) · [原生 Windows 沙箱](../guides/windows-sandbox.md) · [WSL2 使用指南](../guides/wsl2.md) · [权限性能验收](permission-timing.md)
