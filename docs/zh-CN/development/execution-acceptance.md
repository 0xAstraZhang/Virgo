# 原生执行与性能验收

[文档中心](../README.md) · [项目首页](../../../README.zh-CN.md) · [English](../../en/development/execution-acceptance.md)

运行 `uv sync --locked --dev` 安装锁定的依赖。每个一级支持平台（Linux、macOS、原生 Windows 与 Windows + WSL2）均须针对同一候选提交执行原生隔离套件（零失败、零错误、零跳过）并通过真实规模性能预算。

## 原生隔离验收套件

### Linux 与 macOS

在真实的 Linux 主机上运行原生套件，与 `.github/workflows/native-sandbox.yml` 一致。

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q -m native \
  tests/harness/test_sandbox_e2e.py \
  --junitxml=native-sandbox.xml
```

在 macOS 上同时运行 Seatbelt 的 `/tmp` 符号链接授权用例。

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q -m native \
  tests/harness/test_sandbox_e2e.py tests/harness/test_macos_sandbox_e2e.py \
  --junitxml=native-sandbox.xml
```

在任一平台上，校验 JUnit 报告，然后单独运行 Broker 与策略套件。`native` 标记选择实际 OS 沙箱验收；仅测试 Broker 决策或启动协议的用例保留在常规套件中。

新增带有 `native` 标记的测试文件时，须同步更新 Cross-platform CI 中对应平台的显式文件列表，以及相应的手动、Windows 或 WSL2 验收入口，确保新用例被纳入执行。

```sh
uv run --no-sync python ci/check_native_report.py native-sandbox.xml

uv run --no-sync pytest -q -m "not native" \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_boundaries.py \
  tests/harness/test_guardrails.py \
  tests/harness/test_output_store.py
```

### 原生 Windows（标准 AppContainer）

原生 Windows 在已更新的新版 Windows 11（`null.sys 10.0.26100.8972+` / WIL Feature `60692039`，`\Device\Null` 原生授予 `S-1-15-2-1` `0x001201BF`）上运行非提权标准 AppContainer 后端（`windows-appcontainer`，无需管理员权限），并作为 **PR 阻塞的一级支持门槛**：

- GitHub Actions 托管的 Windows Server 2022/2025 runner 默认搭载未启用 Feature `60692039` 的旧版 `null.sys`。Virgo 生产代码**绝不**修改 `\Device\Null`（不受支持的主机默认以 **No-Limit** 启动并给出提示，显式请求沙箱模式时 fail-closed）。
- `windows-sandbox.yml`（`Windows sandbox boundaries`）在 PR 或 `main` 推送包含相关变更、每日定时任务和手动触发时运行 `windows-2022`（原生）以及 `windows-2025` 上的 `standard-user` 任务。相关路径包括 `src/virgo/`、共享测试 fixture/support、Windows 原生测试、Windows 验收/分类脚本与工作流，以及锁定依赖；仅修改 UI、Fetch 或其他无关测试时跳过这两个额外 Windows 任务。两个 runner 均先运行 `ci/prepare_windows_null_device.py --verify-unsupported` 验证原生旧 Server 的默认降级与 fail-closed 行为，再通过 `ci/prepare_windows_null_device.py` 对齐 CI runner 的 `\Device\Null` DACL，随后端到端运行完整标准 AppContainer 隔离套件（含 `NUL`/`subprocess.DEVNULL` 与纯 `*Users` 非管理员账号 `virgo_std` 的 `ci/run_as_standard_user.py --assert-standard-user` 验收），并由 `ci/check_native_report.py` 校验零跳过报告。
- `pr-platform-comparison.yml` 针对已解析的同一候选 SHA，在 `ubuntu-latest`、`windows-latest`（当前为 `windows-2025`，执行同样的 null 设备对齐）与 `macos-latest` 上，将常规（`not native`）和必过原生（`native`）套件放在独立并行任务中运行。汇总要求六份报告和两组任务全部成功；原生报告必须实际执行用例且失败、错误、跳过均为零。常规任务保留 Broker、策略生成和启动协议覆盖。独立的 `native-sandbox.yml` 保留为手动入口，因此 Linux/macOS 原生用例每个 PR 只运行一次。部分重跑按套件/平台选取最新报告，包括失败或不完整的重试；失败任务保留报告并支持“Re-run failed jobs”。
- 纯文档 PR 仍生成成功的汇总检查，不启动常规或原生重套件。文档白名单覆盖根目录 README、`examples/README.md`、`examples/README.zh-CN.md` 和 `docs/` 下的 Markdown；差异为空或不可读取时保守运行测试。Windows 验收在工作流内部按相关路径分类并验证有意跳过的任务，避免工作流级路径筛选造成必过检查缺失；差异为空或不可读取、定时或手动验收时，始终运行两种宿主/账户组合。

如需在非提权 PowerShell 会话中手动运行原生 Windows 验收（使用用户自有或 AAP 可读的原生 x64 Python 3.11+）：

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q -m native tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
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

脚本会检查 `bubblewrap`、运行带 `VIRGO_REQUIRE_SANDBOX_TESTS=1` 的 Linux 原生沙箱测试、通过 `ci/check_native_report.py` 校验 JUnit 报告、单独运行 Broker/策略套件，并校验 **Workspace-Edit** 性能预算。

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

原生进程与网络隔离边界由 `pr-platform-comparison.yml` 中的必过原生任务、`windows-sandbox.yml` 的额外宿主/账户覆盖和 `ci/wsl2_acceptance.sh` 验证；`native-sandbox.yml` 提供手动 Linux/macOS 验收入口。重构期间删除测试时，须在提交信息与 PR 描述中列出每项被删除测试及其替代覆盖。

另见：[执行边界与沙箱](../guides/sandbox.md) · [原生 Windows 沙箱](../guides/windows-sandbox.md) · [WSL2 使用指南](../guides/wsl2.md) · [执行接口](execution-interface.md)
