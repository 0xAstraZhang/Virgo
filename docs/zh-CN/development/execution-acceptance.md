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
