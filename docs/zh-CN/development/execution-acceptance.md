# 原生执行验收

运行 `uv sync --locked --dev` 安装锁定的依赖。

## 一级支持平台（Linux、macOS 与 Windows + WSL2）

在 Linux 或 macOS 主机上运行强制原生沙箱与策略验收套件：

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_v2.py \
  tests/harness/test_safety_v2.py \
  tests/harness/test_output_store.py \
  --junitxml=native-sandbox.xml
```

校验真实仓库规模下的冷启动（<= 5 秒）与单次 `Read` 开销门槛（S5-04 / VER-11）：

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets
```

在 **Windows + WSL2（Ubuntu）** 中，从位于 WSL2 Linux 文件系统内（而非 `/mnt/c`）的仓库根目录执行验收脚本：

```sh
./ci/wsl2_acceptance.sh
```

一级平台须实际执行原生套件，零失败、零错误、零跳过。核对同一候选提交的 JUnit 报告、跨平台测试比较（不再使用绑定 SHA 的退役文件；删除测试须在 PR 描述中声明替代覆盖）与性能门槛。

## 实验性原生 Windows（非阻塞）

原生 Windows LPAC 测试通过每日定时工作流（`windows-sandbox.yml`）运行，不阻塞 PR 合并；非沙箱测试在 `pr-platform-comparison.yml` 的单个 `windows-latest` runner 上作为阻塞门槛运行。如需在管理员权限的 Windows Server 2022/2025 上手动运行实验性 LPAC 套件：

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

记录任何原生 Windows 实测结果时，请一并注明操作系统版本与账户类型（管理员 vs 非管理员桌面账户）。

另见：[执行边界与沙箱](../guides/sandbox.md) · [WSL2 使用指南](../guides/wsl2.md) · [原生 Windows 沙箱（实验性）](../guides/windows-sandbox.md) · [权限性能验收](permission-timing.md)
