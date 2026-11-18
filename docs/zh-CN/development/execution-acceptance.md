# 权限模式验收

验收时核对同一 Git 提交的三类结果。Linux 和 macOS 查看 `Native sandbox boundaries`，Windows Server 2022 和 2025 查看 `Windows sandbox boundaries`。严格原生测试要求实际执行且零失败、零错误、零跳过。`Cross-platform CI` 对比完整测试与 PR 基线。`Permission mode performance` 提供每个平台、每种模式的同机性能比较。

| 模式 | 固定基线 | 改代码和跑测试 |
| --- | --- | --- |
| No-Limit | `98cc711cdb9bbb80a9ad9a2ae11c8d92f94ceec8` 的 Host | Virgo 文件工具修改源码后运行测试 |
| Workspace-Edit | `6346255146f11b8024e11fd3f3faae19f41d5862` 的原生 Workspace-Edit | Virgo 文件工具修改源码后运行测试 |
| Read-Only | `6346255146f11b8024e11fd3f3faae19f41d5862` 的原生工作区只读、Shell 断网配置 | 外部编辑器保存源码，Virgo 运行测试 |

每组在同一台运行器上依次执行两个版本。每个版本取三个样本，每次使用新解释器、独立工作区和用户目录。冷启动覆盖解释器、导入和会话构造，排除准备测试文件的耗时。首条短命令单列，连续五条短命令取总耗时。最后一项从修改源码开始计时，到测试导入新代码并验证返回值为止。成功样本需全程零资源审批。

工作流保存双方原始样本，并检查提交、源码工作树、脚本摘要、平台、Python、负载和样本数。结果包含各项中位数、毫秒差和比值。启动失败保留原因；清理状态未知时停止后续采样。完整样本才能生成比较结果。执行命令见[英文说明](../../en/development/execution-acceptance.md)。

## 原生支持范围与实测结果

POSIX 实时工作区应验证宿主正常编辑对运行中的读取进程立即可见。启动扫描仅检查当时的状态，受支持的边界要求宿主并发写入者维持链接关系的权限范围。

Windows 分别记录外部 TCP 出站、入站和宿主回环结果，同时记录系统、账户和防火墙策略。普通桌面账户需要独立运行。后端发现后的状态为等待实际启动验证。性能数据须绑定源码提交并保留失败样本。

历史 MXC 1.1.0 测试记录了 macOS 共享路径问题、Linux 网络策略限制和 Windows PSEC 不可用。[原始 macOS 记录](../../evidence/execution/macos-mxc-shared-path-probe.json)对应当时的执行器。各版本的测试与性能数据应保留其各自提交和权限约定。

Windows 11 build 26200、x64 Python 3.13.9、未提升权限的桌面账户实测中，实时读取正向用例在载荷启动前被拒绝。创建 KsecDD 设备受保护的协调互斥对象返回 Windows 错误 1307。现有 LPAC 实现依赖这一租约。该账户的出站、入站与回环均未测得。
