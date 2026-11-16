# 独立代码审查

[文档](../README.md) · [English](../../en/guides/code-review.md)

## 开始审查

在 Git 仓库根目录启动 Virgo，然后选择目标：

```text
/review
/review uncommitted
/review base main
/review base refs/remotes/origin/main
/review commit HEAD
/review commit <commit-sha>
/review show last
/review show <review-id>
```

`/review` 是插件唯一的命令。启用 `code_review` 时该名称被保留；存在 `review.md` Prompt 模板会明确报告启动冲突，不会让模板接管命令。请重命名模板或关闭插件后重新启动。旧 `/code-review` 别名和自动命令回退机制已移除。

`coding` 预设启用 `code_review`。Review 是使用正常工具集合和审查指令的独立任务，不为普通命令作审批决定。可通过 `plugins.code_review.enabled` 覆盖预设，例如：

```json
{"profile": "minimal", "plugins": {"code_review": {"enabled": true}}}
```

文本和 JSON 打印模式运行同一个任务：

```bash
uv run virgo -C /path/to/repository -p "/review base main"
uv run virgo -C /path/to/repository -p --format json "/review uncommitted"
uv run virgo -C /path/to/repository --continue -p "/review show last"
```

JSON 模式输出包含 `report` 对象的 `code_review` 事件。报告完成且仍对应当前快照时，退出码为 `0`；执行失败、审查不完整、报告过时或无法核实快照时为 `1`；取消为 `130`。发现缺陷本身不会使执行失败。`--no-session` 仅在内存中保留结果。

## 目标与快照

| 目标 | 固定的比较范围 |
| --- | --- |
| `uncommitted`（默认） | HEAD → index、index → 工作区文件，以及未被忽略的 untracked 文件，分别保留为 `staged`、`unstaged`、`untracked` 层 |
| `base <branch>` | 已解析分支与 HEAD 的唯一 merge-base → HEAD；基线独有的后续提交和工作区脏文件不进入这份 diff |
| `commit <revision>` | 第一父提交 → 指定提交；根提交与空树比较 |

先暂存、再在工作区撤回的改动仍会出现在两个层中。稀疏检出中标记为 `skip-worktree` 的缺失文件保留 index 内容，不会被误判为未暂存删除。Merge commit 使用第一父提交。即使提交位于浅克隆边界，也按提交对象中记录的父提交确定比较范围；本地缺少父提交历史时返回 incomplete。基线必须无歧义地指向本地或远程分支；与 tag 同名时可使用完整 ref。审查不会拉取分支或缺失对象。

插件使用受控 Git argv 解析目标，然后固定文件字节和新旧行映射。Staged 和已提交变更的重命名识别来自 Git 对象/index 比较；unstaged 删除和 untracked 新增仍是独立变更。已跟踪文件被目录替换时，原文件记为删除，未被忽略的新子文件分别记为 untracked 新增。`review_read`、`review_glob`、`review_grep` 选择冻结的 `head`、`index`、`worktree`、`base`、`target` 版本，不会读到随后变化的实时文件。符号链接只暴露链接文本，不跟随其目标。

`core.filemode=false` 只忽略普通文件之间的可执行位差异，文件类型变化仍会保留；`core.symlinks=false` 下以普通文件形式检出的符号链接仍受支持。冻结 diff 保留文件末尾缺少换行符的标记，`read` 提供 `ends_with_newline`，不改变源码行号。

保存的快照记录请求目标、已解析提交、比较方式、变更元数据、文件指纹和快照 ID，用来标识被审查的代码，不是全部源码字节的永久存档。源码内容在任务期间保存在内存中。

## 任务指令与执行权限

每次审查创建来源 Runtime 的独立子 Runtime，复用现有 Agent Loop，继承来源
工作区、模型、请求预算、取消信号和执行权限，不复制父对话。Review 不添加
只读上限，也不强制将审批设为 `never`；即使父对话正在 Plan，它也使用审查
指令。`subagents.maxTurns` 限制轮数，正常的上下文压缩仍然可用。

子 Runtime 的正常工具集合全部保留。审查指令要求调查并报告问题，不实施
修复、提交或合并。实际文件、Shell、网络访问由当前权限预设 决定。需要
阻止源文件写入时，应独立选择 Read-Only。模型如果违反审查指令，在可写
权限预设中仍可能执行获准写入；Review 不构成另一层安全边界。

冻结证据通过 `review_read`、`review_glob`、字面量 `review_grep` 和 `review_git`
提供，与读取实时文件的普通工具名称区分。`review_git` 只分页读取快照的
status/diff，不接受任意命令或 Git 参数。普通工具可以补充调查上下文，但
Finding 的位置始终按冻结目标校验。

子任务采用普通扩展 hooks、请求准备和动态工具更新。审查指令在每次模型
请求时重新应用，包括压缩之后。未经校验的助手 JSON 不直接进入原始消息
视图；客户端显示经校验的报告，同时保留工具和活动事件。普通输出限制、
会话元数据和诊断记录仍然适用。

两个权限检查点仍然生效。快照读取使用捕获的来源权限。捕获受阻时返回
incomplete，不静默扩大权限；无法完成 freshness 检查时为 unknown。模型
调查期间可以在 `ask` 策略下通过 `request_permissions` 请求一次精确操作。
取消会释放等待和子 Runtime；`never` 会拒绝新权限申请，不弹出审批。

受控快照 Git 读取仍关闭可选 index 写入、external diff、textconv、fsmonitor、
hooks 和隐式联网。工作文件直接比较捕获字节，不执行 clean/smudge filter。
这些是快照读取器自身的操作定义，不限制普通 Shell 工具。可信 Python 扩展
在宿主进程中执行；启动 Review 不会停止之前的后台任务。

## 报告与 Finding 定位

| 状态 | 含义 |
| --- | --- |
| `running` | 正在采集或审查 |
| `completed` | 完整报告通过校验，或精确目标没有变更 |
| `invalid_report` | JSON、字段、类型或 Finding 定位不合法 |
| `incomplete` | 上下文、轮数/请求预算或模型输出不足以形成完整结论 |
| `failed` | 目标选择、执行、模型服务或清理失败 |
| `cancelled` | 任务已观察到取消 |

报告包含 `id`、`status`、`snapshot`、`summary`、`findings`、时间和 freshness；有可用诊断时，错误会保留相关证据。每条 Finding 包含 `change_id`、`path`、`side`、`start_line`、`end_line`、`priority`（整数 0–3）、`title`、`explanation` 和有限数值 `confidence`（0–1）。Change ID 区分同一文件的不同层，`side` 区分旧/删除行与新/新增行。定位必须匹配确切路径与侧别、在对应版本中存在、覆盖变更行，且最多 20 行。源码行号只以 LF 分行；嵌入的 Unicode 分隔符、垂直制表符、换页符和独立 CR 字符不会增加行号。

审查提示要求报告目标变更引入的、证据明确且可操作的缺陷并说明影响，排除已有问题、推测和风格偏好。结构校验不能证明模型推理本身正确。非法输出、拒绝、截断、溢出、证据缺失和取消不会转换成成功的空 Findings。界面展示经过校验的报告，不直接展示原始模型结论。

## 历史结果与当前限制

生命周期和结果以 `code_review` 自定义记录保存在来源会话分支中，不进入父模型对话。`/review show` 不调用模型，重新检查指纹并显示 `current`、`stale` 或 `unknown`，保留原始结论和快照。取消该检查时返回退出码 `130`，显示结果包含 `freshness: "unknown"` 和临时字段 `freshness_cancelled: true`，已保存的结果不变。Freshness 包含工作区/index 上下文，因此工作区后续变化可以使固定 commit 的报告显示 stale，即使该 commit 本身不可变。重新打开没有活跃任务的 `running` 记录时，会显示 incomplete。

发布最终报告前也会检查 freshness。采集时检测到 ref/index/文件变化会拒绝混合快照，但不会锁住宿主并发写入。修改代码后应重新审查。正常取消会终止并回收受控 Git 进程，释放子 Runtime 注册、权限关系和持久化失败信号。

当前限制为每个文件 1 MiB、捕获内容总计 32 MiB、每个版本 10,000 个路径。工具每页最多 400 行，结果低于 45 KiB。所选变更之外的超限文件保留为不可用上下文，不会仅因它存在就中断固定 commit 的审查。必要证据超限或不可用时返回 incomplete，包括明确读取或搜索需要其内容的情况。变更中的二进制/非 UTF-8 内容、未解决的 index 冲突和未提交 submodule 内容不在本次文本审查范围内。不支持的 clean filter、`working-tree-encoding`、`ident` 转换会被报告为上下文缺失，不会执行转换程序；标准文本/换行元数据由快照比较处理。宽范围搜索遇到不可读/二进制内容会列出跳过的证据并标记审查不完整；需要时可将搜索范围限定在可读源码路径。

Diff 匹配会检查取消信号，并为每个变更文件检查五秒截止时间。相同前后缀直接处理；剩余匹配搜索超过 1,000,000 个候选行对时返回 incomplete。展示的 diff 片段与 Finding 行号校验使用同一次有界比较。

纯重命名、权限位变化会被记录，但本增量不能为没有变更文本行的情况附加 Finding 行锚点。仅拥有子目录工作区的 Runtime 不能扩大范围到仓库根目录。本命令没有自动修复、独立 review model 配置、补丁应用、PR 发布或多审查任务编排。

实现入口：[目标与工具](../../../src/virgo/plugins/code_review/targets.py)、[校验与展示](../../../src/virgo/plugins/code_review/report.py)、[任务插件](../../../src/virgo/plugins/code_review/__init__.py)、[共享受控 Git 读取](../../../src/virgo/tools/git_read.py)。[E2E 验收场景](../../../tests/harness/test_code_review_e2e.py)使用真实 Git、会话和工具循环，仅模型响应由脚本提供；它们验证权限继承和报告行为，不证明模型发现缺陷的质量。
