# 权限模式验收

验收时核对同一 Git 提交的三类结果。Linux 和 macOS 查看 `Native sandbox boundaries`，Windows Server 2022 和 2025 查看 `Windows sandbox boundaries`。严格原生测试要求实际执行且零失败、零错误、零跳过。`Cross-platform CI` 对比完整测试与 PR 基线。`Permission mode performance` 提供每个平台、每种模式的同机性能比较。

| 模式 | 固定基线 | 改代码和跑测试 |
| --- | --- | --- |
| No-Limit | `98cc711cdb9bbb80a9ad9a2ae11c8d92f94ceec8` 的 Host | Virgo 文件工具修改源码后运行测试 |
| Workspace-Edit | `6346255146f11b8024e11fd3f3faae19f41d5862` 的原生 Workspace-Edit | Virgo 文件工具修改源码后运行测试 |
| Read-Only | `6346255146f11b8024e11fd3f3faae19f41d5862` 的原生工作区只读、Shell 断网配置 | 外部编辑器保存源码，Virgo 运行测试 |

每组在同一台运行器上依次执行两个版本。每个版本取三个样本，每次使用新解释器、独立工作区和用户目录。冷启动覆盖解释器、导入和会话构造，排除准备测试文件的耗时。首条短命令单列，连续五条短命令取总耗时。最后一项从修改源码开始计时，到测试导入新代码并验证返回值为止。成功样本需全程零资源审批。

工作流保存双方原始样本，并检查提交、源码工作树、脚本摘要、平台、Python、负载和样本数。结果包含各项中位数、毫秒差和比值。启动失败保留原因；清理状态未知时停止后续采样。完整样本才能生成比较结果。执行命令见[英文说明](../../en/development/execution-acceptance.md)。

POSIX 实时工作区需核对并发链接变更的支持条件。Windows 分别记录外部 TCP 出站、入站、宿主回环与普通桌面账户的验证结果。Windows Server 报告注明其运行账户和环境。历史 MXC、模型评测和旧测时结果按各自提交归档。[历史证据](../../evidence/execution/README.md)
