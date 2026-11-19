# 原生执行验收

运行 `uv sync --locked --dev` 安装锁定的依赖。在受支持的 Linux 或 macOS 主机运行：

```sh
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q tests/harness/test_sandbox_e2e.py --junitxml=native-sandbox.xml
```

在 Windows Server 2022 或 2025 上使用 PowerShell：

```powershell
$env:VIRGO_REQUIRE_SANDBOX_TESTS = "1"
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

各平台须实际执行原生套件，零失败、零错误、零跳过。核对同一候选提交的 JUnit 报告和完整跨平台比较。验证运行中的命令可以读取刚保存的修改，切换模式后活动任务仍使用原权限，撤销的精确授权在下一次调用时失效。记录操作系统和账户类型；Windows 另分别记录出站、入站和宿主回环连接结果。[POSIX 实时工作区约定](../guides/sandbox.md) · [Windows 要求与已知限制](../guides/windows-sandbox.md)

[权限性能验收](permission-timing.md)将三个模式分别与固定主线提交对照，同时记录审批次数。

## Windows 完整比较

跨平台工作流对每个 Windows 修订使用两个独立 runner。`windows-core` 保留既有经过审查的并行组和串行其余用例，只排除 `tests/harness/test_windows_sandbox_e2e.py`；`windows-native` 在另一台主机串行执行该完整模块。Core 仍可能包含原生调用，例如既有审查过的并行组中的 TUI 审批用例；它们的执行安排不变。同一 Windows 主机不新增并行 ACL 测试，完整比较不删除任何测试。Linux、macOS 的执行安排和所有任务时限保持不变。

两片必须都完整结束，并且修订、Python 和系统身份一致，才会合并结果。缺片、未完成、重复报告或重叠测试身份均导致比较失败。head 的原生片必须零失败、零错误、零跳过；baseline 正常结束但包含失败时，保留原始结果供原有比较逻辑区分既有失败和已通过用例，不把它误报为缺片。合并后的完整测试清单仍经过原有退休映射、新增跳过和已执行身份消失的门禁。

`results-windows-latest-<revision>-core` 和 `-native` artifact 保留各 runner 的原始 JUnit、日志和标准化结果。comparison artifact 另含 `comparison-windows-latest-<revision>.json`，记录全部合并测试结果以及每片的耗时、完整标志和退出码。汇总中的 Windows 秒数是两个 runner 执行耗时之和，不是工作流实际耗时或产品性能测量；未完成的重跑不能复用先前的完整合并报告。

`python ci/checks/parallel.py` 使用真实 pytest 子进程验证分片和合并约定，包括保留失败与拒绝不完整输入。这些小型 CI fixture 不能替代上方要求的原生平台验收。
