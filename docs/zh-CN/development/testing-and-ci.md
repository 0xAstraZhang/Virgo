# 开发与 CI

[文档首页](../README.md) · [English](../../en/development/testing-and-ci.md)

## 本地流程

在仓库根目录运行：

```bash
uv sync --locked --dev
uv run --no-sync ruff check
uv run --no-sync pytest -q
uv build
```

日常迭代优先选择受影响的现有场景。真实 POSIX 伪终端测试使用 `pty` 标记，`uv run --no-sync pytest -q -m "not pty"` 可排除它们。PowerShell 场景需要相应解释器，不可用时会跳过。本机通过不等价于 Windows 或 macOS 行为已经验证。

优先通过端到端场景检查可观察行为。只有重要失败方式需要覆盖时才增加场景，并在实现前先描述可能的失败，不应仅为复述代码而增加测试。现有套件使用 faux provider、本地 HTTP 服务、隔离配置和终端驱动，常规验证不需要真实模型凭据。

测试按源码职责组织在 `tests/ai`、`agent`、`harness`、`features`、`tools` 和 `cli`。共享夹具和替身位于 `tests/support/`，测试模块之间不互相导入。跨平台 Shell 引号使用 `python_command`，写入使用临时目录；子进程从其他目录运行时，使用被测 checkout 的精确源码路径。离线场景应隔离持久化用户配置和外部目录刷新。

保留保护独立行为、失败方式或集成边界的场景。只有现有保留场景已经覆盖同一行为时，才删除较弱的重复用例，不能仅因用例小或只测试单个模块而删除。[测试退役记录](test-retirements.md)列出已删除的 9 个重复用例、保留覆盖和 CI 认可删除的条件。

## 自动检查

[跨平台 CI](../../../.github/workflows/pr-platform-comparison.yml) 在 PR 和推送 `main` 时运行：解析事件中的不可变版本，执行 lint、工作流和 CI 自身验收，按需运行产品套件，最后报告比较检查。

| 触发方式 | 产品验证 |
| --- | --- |
| Pull request | 在 Linux、Windows、macOS 上比较事件中的精确 base 和 head；拒绝新增失败、未经审批的测试移除和基线用例新增跳过 |
| 推送 `main` | 在三个平台严格验证推送的 head |
| 手动触发 | 一次性解析指定 PR，并运行完整比较 |
| 仅允许的 Markdown 文档路径变化 | 执行轻量检查，由最终检查报告跳过全量套件 |

纯文档分类包含根目录两份 README 和 `docs/` 下的 Markdown。分类禁用重命名检测，因此将源码移入文档仍会检查新旧两个路径。历史缺失、diff 失败、空变更或其他文件变化都保守地运行全量测试。最终比较检查仍会执行，前置任务缺失或失败不能伪装成文档检查成功。

CI 当前使用 Python 3.13 和锁定依赖，软件声明的最低版本仍为 Python 3.11。Actions 固定到提交 SHA，具体工具版本、超时和产物保留时间维护在工作流中。Linux head 任务构建 sdist／wheel，并在 checkout 外安装 wheel 后检查 CLI，以发现包内容遗漏。

## 阅读结果

最终摘要和 `comparison` 产物列出新失败、已有失败、修复、跳过变化和新增／缺失场景。每份 `results-<平台>-<版本>` 包含日志、JUnit XML、标准化结果、解释器信息和被测 SHA。失败详情包含场景、源码位置和堆栈，不输出 locals。

PR 比较区分“基线通过后回归”和“基线跳过或不存在、现被暴露的失败”，两者都会失败。基线已经失败的场景保留展示，但单独存在时不会让 PR 比较失败。Main 的单版本严格模式对任何 failure/error 都失败。

基线曾执行的测试未经审批而消失或变成跳过、收集失败、运行不完整、产物损坏或 SHA 不匹配，都会导致检查失败。场景身份使用函数／方法名加参数 ID，不依赖模块路径；重复身份会被拒绝。获认可的历史改名明确记录在 [`reports.py`](../../../ci/pipeline/reports.py)，未知改名不会静默接受，别名也不能掩盖回归。

### 经审查的重复测试退役

主动删除重复用例需要在 `ci/pipeline/reports.py::APPROVED_RETIREMENTS` 显式登记，范围限定为经审查的精确基线 SHA、测试 identity 和原始模块。仅当被删除用例在该基线通过，且每个列出的保留场景在 base/head 均通过时，例外才生效。基线失败、保留覆盖缺失／跳过／失败、其他基线或无关同名用例都不能使用该例外。旧用例仍存在于 head 时，继续接受常规失败／跳过检查。

删除项仍列入 `removed`，`retired` 在 JSON 和可读摘要中记录其保留覆盖。这独立于身份别名，不减少预期数量，也不跳过测试执行。各条目的理由见[退役记录](test-retirements.md)。如果 PR 合并前基线改变，需针对新基线审查映射并显式更新精确 SHA；合并后，后续基线不再包含这些用例，旧例外无法适用。

### 经审查的产品退役

移除官方产品使用独立精确清单 [`core_plugin_retirements.json`](../../../ci/pipeline/core_plugin_retirements.json)，不属于重复覆盖豁免。基线 `47e1757` 的 46 个原场景必须通过，五个保留能力门槛须在 base/head 通过，八个新迁移／Core 验收门槛须在 head 通过。原始模块和参数展开后的身份必须匹配；错误基线、无关删除和仍存在的失败／跳过继续阻断通过。

这些身份仍列于 `removed`，由独立 `retired_products` 解释，与重复清理的 `retired` 区分。[测试退役记录](test-retirements.md#core插件的产品退役)说明产品理由，JSON 清单列出每个精确身份。更换基线后需重新审查清单，不按目录接受所有删除。

## CI 代码结构

| 路径 | 职责 |
| --- | --- |
| `ci/compare_tests.py` | 稳定的 `resolve`、`classify`、`run`、`compare` CLI |
| `ci/pipeline/targets.py` | 不可变版本解析和变更路径分类 |
| `ci/pipeline/execution.py` | 执行套件并生成报告 |
| `ci/pipeline/reports.py` | 报告完整性、认可的身份别名和经审查的重复测试退役 |
| `ci/pipeline/comparison.py` | 回归策略与最终摘要 |
| `ci/pipeline/io.py` | 严格 JSON 和工作流输出 |
| `ci/run_suite.py`、`ci/run_pytest.py` | 套件分组和进程环境 |
| `ci/parallel-files.json` | 经审查的并行分组 |
| `ci/checks/` | CI CLI 行为的端到端验收 |

整理内部结构时保持工作流文件名、CLI 选项、产物和任务身份稳定。决策／报告流水线只使用标准库，并从不可变的 CI 工具 checkout 运行，与被测 base/head checkout 分离。

## 运行器维护

Unix 上，`ci/run_pytest.py` 为每个 pytest 进程提供独立 `XDG_CACHE_HOME`，创建受保护的只读 PowerShell 启动 profile 文件，并在子进程启动前关闭 PowerShell telemetry，防止并发写启动 profile 和冷缓存 telemetry 初始化触发已知的原生启动故障。其他缓存仍可写，`HOME` 不变，临时目录会清理，pytest 失败会正常传播。该保护需要非 root 运行器。Windows 保留正常环境。

这是 CI 进程环境措施，不增加重试，也不改变普通 Virgo 会话。`ci/checks/powershell_cache.py` 需要真实 `pwsh`，检查并发启动和环境清理。背景依据也记录在上游 [PowerShell 启动 profile 问题](https://github.com/PowerShell/PowerShell/issues/26528)中。

两个白名单内的隔离测试分组在不同进程执行，每组内部串行；其余套件继续串行正常发现，因此新文件与 Shell／PTY 测试在审查前保持串行。旧基线不存在的白名单文件会忽略，重复项会失败。调查时可以给运行器传 `--execution serial`。

修改运行器或比较策略前，先执行现有 CLI 验收：

```bash
uv run --no-sync python ci/checks/resolution.py
uv run --no-sync python ci/checks/parallel.py
uv run --no-sync python ci/checks/comparison.py
uv run --no-sync python ci/checks/powershell_cache.py
```

这些检查驱动真实临时项目，覆盖错误版本、缺失／不完整结果、前置任务假绿、身份冲突、测试丢失和运行器失败。新增失败边界需要覆盖时，在修改实现前补足，而非实现后补写。

退役机制验收还必须防范无关删除、基线失败、新增跳过，以及保留覆盖缺失／跳过／失败；必须拒绝将例外复用于其他基线 SHA 或无关同名测试，并检查被删除的 identity 是否从可读或 JSON 比较报告中消失。

## 手动诊断

独立的 [Shell 诊断工作流](../../../.github/workflows/shell-selection.yml)仅手动触发：

```bash
gh workflow run pr-platform-comparison.yml -f pr_number=<number>
gh workflow run shell-selection.yml
gh workflow run shell-selection.yml -f powershell_diagnostics=true
gh workflow run shell-selection.yml --ref <branch> -f benchmark_suite=true
```

基准比较在同一运行器和提交上分别执行串行、并行全套测试，并验证逐项结果一致。审查白名单变更时使用其产物。单次耗时和事故调查保留在对应 PR，仅将后续维护需要的结论更新到本指南。
