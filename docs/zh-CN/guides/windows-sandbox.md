# Windows 执行支持状态

默认 Host。Host 进程保留 Windows Job Object 生命周期管理，但它不是隔离后端。

旧 LPAC token/DACL 沙箱已移除。显式 Sandbox 只使用固定的 MXC 1.1.0 `wxc-exec.exe`，指定 processcontainer、leastPrivilege，禁用 DACL fallback 和宽松审计。不自动下载，也不回退 Host。

**Windows 替换未通过验收。** 此前 Windows Server 2022／2025 评估因 PSEC 不可用而无法启动。当前共享宿主路径别名策略也未获支持。能力探测、预先拒绝、模拟结果不能作为真实启动和隔离证据。

文件工具保留无重解析点的句柄遍历、新 inode 替换和权限保留；其结果与原生 Shell 隔离独立。真实平台验收必须包含成功启动、允许及禁止操作，零失败、错误和跳过：

```powershell
$env:VIRGO_MXC_EXECUTABLE = 'C:\installed\wxc-exec.exe'
$env:VIRGO_REQUIRE_SANDBOX_TESTS = '1'
uv run --no-sync pytest -q tests/harness/test_windows_sandbox_e2e.py --junitxml=windows-sandbox.xml
```

当前该 gate 继续阻塞，参见[验收记录](../development/execution-acceptance.md)。
