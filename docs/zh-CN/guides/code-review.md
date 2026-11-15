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

`/code-review` 接受相同参数。插件在启动时只注册未被命令或 Prompt 模板占用的名称，并明确提示冲突。如果已有 `review.md` 提供 `/review`，请使用 `/code-review`；如果两个名称均被占用，需要先重命名一个冲突的模板或命令，再重新启动。已有用户文件会保留。

`coding` 预设启用 `code_review`，它与辅助权限审批的 `auto_review` 相互独立。可通过 `plugins.code_review.enabled` 覆盖预设，例如：

```json
{"profile": "minimal", "plugins": {"code_review": {"enabled": true}}}
```

文本和 JSON 打印模式运行同一个任务：

```bash
uv run virgo -C /path/to/repository -p "/review base main"
uv run virgo -C /path/to/repository -p --mode json "/review uncommitted"
uv run virgo -C /path/to/repository --continue -p "/review show last"
```

JSON 模式输出包含 `report` 对象的 `code_review` 事件。报告完成且仍对应当前快照时，退出码为 `0`；执行失败、审查不完整、报告过时或无法核实快照时为 `1`；取消为 `130`。发现缺陷本身不会使执行失败。`--no-session` 仅在内存中保留结果。

## 目标与快照

| 目标 | 固定的比较范围 |
| --- | --- |
| `uncommitted`（默认） | HEAD → index、index → 工作区文件，以及未被忽略的 untracked 文件，分别保留为 `staged`、`unstaged`、`untracked` 层 |
| `base <branch>` | 已解析分支与 HEAD 的唯一 merge-base → HEAD；基线独有的后续提交和工作区脏文件不进入这份 diff |
| `commit <revision>` | 第一父提交 → 指定提交；根提交与空树比较 |

先暂存、再在工作区撤回的改动仍会出现在两个层中。Merge commit 使用第一父提交。基线必须无歧义地指向本地或远程分支；与 tag 同名时可使用完整 ref。审查不会拉取分支或缺失对象。

Harness 使用受控 Git argv 解析目标，然后固定文件字节和新旧行映射。Staged 和已提交变更的重命名识别来自 Git 对象/index 比较；unstaged 删除和 untracked 新增仍是独立变更。读取、搜索工具选择冻结的 `head`、`index`、`worktree`、`base`、`target` 版本，不会读到随后变化的实时文件。符号链接只暴露链接文本，不跟随其目标。

保存的快照记录请求目标、已解析提交、比较方式、变更元数据、文件指纹和快照 ID，用来标识被审查的代码，不是全部源码字节的永久存档。源码内容在任务期间保存在内存中。

## 只读执行

每次审查创建来源 Runtime 的独立子 Runtime，复用现有 Agent Loop，继承来源工作区、模型、共享请求预算、取消信号和权限关系，不复制父对话。`subagents.maxTurns` 限制轮数；审查关闭压缩，上下文溢出会成为不完整结果。

审查仅提供 `read`、`glob`、字面量 `grep` 和 `review_git`。`review_git` 只分页返回冻结的 status/diff，不接受任意命令或 Git 参数。工具集合不包含文件修改、Shell、联网、MCP、后台任务或委派能力，任务不能修复、提交或合并代码。

命令绕过 Prompt hooks；Runtime 排除可执行的工具/停止 hooks、上下文/系统提示注入、动态工具注册和模型托管联网能力。Core 的两个权限检查点仍生效，快照内容遵守继承的读取限制。`accept-all`、allow 规则和批准不能增加工具。结果在内存中分页，不创建 `.virgo/outputs` 文件。

受控 Git 读取关闭可选 index 写入、external diff、textconv、fsmonitor、hooks 和隐式网络传输。工作文件通过已捕获字节比较，不执行 clean/smudge filters。生命周期、结果和诊断使用 Virgo 的用户/会话位置；这些目的地解析到被审查工作区内部时，任务会拒绝启动。

这是官方任务的能力边界。受信任的 Python 扩展仍在宿主进程中运行，其初始化和事件观察者不受本任务沙箱隔离。启动审查也不会停止此前已经运行的独立后台任务。

## 报告与 Finding 定位

| 状态 | 含义 |
| --- | --- |
| `running` | 正在采集或审查 |
| `completed` | 完整报告通过校验，或精确目标没有变更 |
| `invalid_report` | JSON、字段、类型或 Finding 定位不合法 |
| `incomplete` | 上下文、轮数/请求预算或模型输出不足以形成完整结论 |
| `failed` | 目标选择、执行、模型服务或清理失败 |
| `cancelled` | 任务已观察到取消 |

报告包含 `id`、`status`、`snapshot`、`summary`、`findings`、时间和 freshness；有可用诊断时，错误会保留相关证据。每条 Finding 包含 `change_id`、`path`、`side`、`start_line`、`end_line`、`priority`（整数 0–3）、`title`、`explanation` 和有限数值 `confidence`（0–1）。Change ID 区分同一文件的不同层，`side` 区分旧/删除行与新/新增行。定位必须匹配确切路径与侧别、在对应版本中存在、覆盖变更行，且最多 20 行。

审查提示要求报告目标变更引入的、证据明确且可操作的缺陷并说明影响，排除已有问题、推测和风格偏好。结构校验不能证明模型推理本身正确。非法输出、拒绝、截断、溢出、证据缺失和取消不会转换成成功的空 Findings。界面展示经过校验的报告，不直接展示原始模型结论。

## 历史结果与当前限制

生命周期和结果以 `code_review` 自定义记录保存在来源会话分支中，不进入父模型对话。`/review show` 不调用模型，重新检查指纹并显示 `current`、`stale` 或 `unknown`，保留原始结论和快照。Freshness 包含工作区/index 上下文，因此工作区后续变化可以使固定 commit 的报告显示 stale，即使该 commit 本身不可变。重新打开没有活跃任务的 `running` 记录时，会显示 incomplete。

发布最终报告前也会检查 freshness。采集时检测到 ref/index/文件变化会拒绝混合快照，但不会锁住宿主并发写入。修改代码后应重新审查。正常取消会终止并回收受控 Git 进程，释放子 Runtime 注册、权限关系和持久化失败信号。

当前限制为每个文件 1 MiB、捕获内容总计 32 MiB、每个版本 10,000 个路径。工具每页最多 400 行，结果低于 45 KiB。必要证据超限或不可用会产生 incomplete。变更中的二进制/非 UTF-8 内容、未解决的 index 冲突和未提交 submodule 内容不在本次文本审查范围内。不支持的 clean filter、`working-tree-encoding`、`ident` 转换会被报告为上下文缺失，不会执行转换程序；标准文本/换行元数据由快照比较处理。宽范围搜索遇到不可读/二进制内容会列出跳过的证据并标记审查不完整；需要时可将搜索范围限定在可读源码路径。

纯重命名、权限位变化会被记录，但本增量不能为没有变更文本行的情况附加 Finding 行锚点。仅拥有子目录工作区的 Runtime 不能扩大范围到仓库根目录。本命令没有自动修复、独立 review model 配置、补丁应用、PR 发布或多审查任务编排。

实现入口：[目标与工具](../../../src/virgo/harness/code_review_targets.py)、[校验与展示](../../../src/virgo/harness/code_review_report.py)、[任务插件](../../../src/virgo/plugins/code_review.py)。[E2E 验收场景](../../../tests/harness/test_code_review_e2e.py)先于生产实现提交。
