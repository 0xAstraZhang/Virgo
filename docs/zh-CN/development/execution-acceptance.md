# 执行验收与阻塞

本次实现采用默认 Host、显式 MXC Sandbox，不能将组件测试概括为“全部隔离已通过”。完整证据与复现命令见[英文验收记录](../../en/development/execution-acceptance.md)。

| 范围 | 当前结论 |
| --- | --- |
| Host/Core | 独立 Python 进程测试真实读写、复杂 Shell 和普通子代理，并禁止导入 Sandbox；没有普通审查或审批。来源、单次授权、UI 与事实保留分别验证。 |
| 文件工具 | 保留句柄固定、新 inode 替换、编码／权限、失败编辑和硬链接 unlink 竞争用例；Windows 2022/2025 记录中 3 个真实 NTFS broker 别名场景通过；仍不能替代原生 Shell 验收。 |
| macOS MXC | **未通过**。1.1.0 ARM64 实际启动和允许写入成功，启动后硬链接别名却能修改工作区外文件。[原始报告](../../evidence/execution/macos-mxc-shared-path-probe.json)保留了失败。 |
| Linux MXC | **未验收**。共享宿主路径策略在载荷启动前拒绝；没有本地 Linux 原生成功证据。 |
| Windows MXC | **未验收／阻塞**。此前 2022／2025 主机因 PSEC 不可用无法启动；与共享路径策略不支持分别记录，不回退旧 LPAC。 |
| 严格 macOS gate | 本地运行 36 通过、32 个初始化错误、零跳过；错误是原生策略未获支持，不能计为隔离成功。 |
| 提示词效果 | 用户指定 Codex `gpt-6.1-sol`、medium；[3/3 任务正确、审批为零，未观察到意图违反或越界尝试](../../evidence/execution/instruction-summary.json)。[全部调用、输出、耗时和 token](../../evidence/execution/macos-codex-instructions.json)已保留。仅限此小规模 Host 任务集，不代表原生隔离或通用模型质量，没有恢复暂停的 Virgo/Codex 对比评测。 |
| 性能 | 保留全部 Host／Sandbox 样本。失败不计成功执行延迟，两条路径权限不同，不能推断同权限后端优势。 |
| 维护成本 | 分开记录移动、保留、删除／重写、新增适配与依赖，未声称维护成本已经下降。 |

本地工作区报告记录源文件摘要，不能替代同一 PR 最新 head 的 lint、构建、完整测试和跨平台比较。所需原生用例必须真实启动并通过正反对照，零失败、错误、跳过；当前 gate 继续阻塞。

原生复现只操作临时测试资源，直接调用适配器调查 MXC，明确绕过产品“不支持策略”入口。它不是用户执行回退。启动前扫描、模型主动守规矩、网络整体失效和后端探测都不能证明隔离。

macOS 清理采用尽力契约；后代退出的个例不会升级为普遍保证。缺少回执时保留 unknown，清理失败明确报告，不重放有副作用的操作。可信 Python、hooks、MCP 启动和模型通信仍在受管范围外。

## 固定版本的跨平台原生记录

[原始报告及归档摘要](../../evidence/execution/native-60df626/summary.json)绑定行为提交 `60df62697f98949ead0c92669098ec84136e2d4e`。Linux/macOS 各 36 个 broker／协议场景通过、32 个原生准入错误；Windows 2022/2025 各 3 个 NTFS broker 别名场景通过、13 个原生准入错误，均零跳过。这些通过项不代表原生隔离通过。

独立上游 probe 中，macOS 26.7.2 启动成功并复现别名绕过；Linux 明确拒绝不支持的 `network.ingress.default=allow`；两类 Windows 主机均报告 PSEC 未启用。没有为了获得成功启动而放宽或改写请求的网络策略。[旧版本样本](../../evidence/execution/945bdfa/)独立保留，不与新版本合并比较。

此前 [Anthropic 尝试](../../evidence/execution/macos-instruction-attempt.json)因缺少凭据停止；用户随后选择现有 Codex 登录。新 manifest 只更换模型，任务、medium 强度及 Host 权限保持一致，凭据不写入结果。这些是独立尝试，不用于模型间比较。
