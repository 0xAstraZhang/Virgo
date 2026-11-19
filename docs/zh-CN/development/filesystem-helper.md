# 文件 Helper 与输出记录

内置 Read、Write、Edit、Glob、Grep 通过 `SandboxExecutor` 启动独立 Python 进程。工具适配器验证参数、转换结果，将目标路径原样交给 Helper。文件内容、元数据和目录遍历均由 Helper 操作。执行器在模型开始操作前保存仅依赖标准库的可信运行时副本；从 Virgo 自身的开发目录启动时也使用这个副本。审批仅规范化资源路径的文本表达，并将有效 `PermissionPolicy` 交给执行器，不在 Broker 中解析或探测模型指定的目标。

所有模式使用同一种不可变 `ExecutionContext`，其中保存权限资源、执行器、来源权限快照和输出存储。No-Limit 明确构造无限制上下文。自动项目读取调用 `read_context(context, ...)`、`read_file_bytes(context, ...)` 或 `walk_file_entries(context, ...)`，由它们启动 Helper 操作。上下文不提供直接文件访问方法。可信用户指令和 Virgo 配置仍由 Broker 读取。

副本位于操作系统用户缓存中的稳定 `virgo-runtime` 父目录，与 Broker 私有存储和命令临时目录分开。初始化拒绝受管理目录中的链接、错误所有者和非私有权限，并保留当前 Windows 执行的 ACE。所有受限进程均不得修改整棵运行时目录，包括其他执行器随后创建的副本；原生读取授权覆盖当前进程所需的运行时依赖。关闭执行器仅删除自身副本，保留父目录，使已经运行的进程继续受到针对未来实例的保护。No-Limit 保持明确授权的宿主访问权限。

Broker 首次加载 Helper 适配模块时，在首个执行器可以启动模型操作之前，一次性捕获 Helper 源码字节。后续执行器复用已捕获的内容，不再读取可能已被修改的开发安装目录。Helper 实现的修改在受信任的 Broker 重启后生效。

## 传输与大小限制

执行器以 `-I -S` 启动 Helper。请求和响应使用私有 stdin/stdout 管道，每帧由四字节长度和 JSON 正文组成，最大 16 MiB。stderr 单独传递有大小上限的诊断信息。载荷不从模型可写的项目文件中获取执行请求。

文本读取最多返回 2,000 行、约 50 KiB 正文，再附分页提示。图片限制为 5 MiB。Grep 跳过超过 5 MiB 的文件，并限制展示结果。内部项目内容读取最多返回 8 MiB，内部目录枚举最多返回 10,000 项，并明确标注截断。自动指令读取拒绝任一路径组件中的符号链接。

目录搜索只枚举普通文件，不跟随文件或目录别名，因此 Glob 和递归 Grep 忽略符号链接项。Windows 拒绝作为显式授权根、拒绝根或工作目录的重解析点；普通 ACL 授权遍历跳过后代别名，不授权其目标。

自动项目指令、技能发现、提示模板各自使用一次内部 `read_context` 请求，并继续携带该组件捕获的权限上下文。这是专用只读采集操作，不注册为模型工具，也不接受任意操作列表。路径解析、目录遍历和读取仍由 Helper 完成；各组件保留原有正文解码与优先级规则。空项目创建会话时的 Helper 启动次数由七次减少为三次。后续提示重建和技能发现仍逐次启动新进程，不跨任务或跨权限策略缓存内容。

一次采集最多接收 256 个显式路径和 16 个目录选择项，最多返回 256 个文件条目、扫描 10,000 个文件条目。目录选择仅支持直属 `*.md` 模板或下一层 `*/SKILL.md` 技能。单文件及所有返回原始正文的总量均不超过 8 MiB；文件条目序列化后的总量另限 12 MiB，并继续受 16 MiB 协议帧上限约束。文件缺失、访问被拒绝或单项读取失败分别返回错误，不丢弃其他输入。超限正文整体省略，以单文件错误或整次采集的截断标记说明，不静默保留不完整的指令。调用方不会自动继续采集或扩大权限。

执行器等待 Helper 完成，并复用 Shell 的进程清理机制。取消或输出失败发生时，写操作可能已经创建目录或提交替换，因此保留“可能已有副作用”的信息，不自动重放。

## 原子修改与权限

Write 和 Edit 保留目录句柄，以不跟随链接的方式打开普通文件。编辑匹配全部成功后，在目标目录创建临时文件，再原子替换目标目录项。替换被拒绝时，不退回截断旧 inode 的实现。POSIX 保留现有文件模式以及既有 BOM、CRLF 编辑行为。

POSIX 路径遍历使用仅用于路径查找的描述符逐级保留祖先目录（Linux 为 `O_PATH`，macOS 为 `O_SEARCH`），保留所有路径组件的不跟随链接检查，不请求枚举祖先目录。目录枚举会单独以读取权限打开目标目录。创建父目录时先打开现有组件，仅对缺失组件调用 `mkdir`，因此写入工作区文件不会请求修改其已有祖先目录。

临时文件位于目标目录，因此不跨文件系统，也不要求项目 `.virgo` 可写。原子修改需要创建、重命名目标父目录中的目录项。单个现有文件的授权可以支持 Shell 对该 inode 的写入，但不足以支持 Helper 的原子替换。需要时应明确申请父目录权限，不能静默扩大单文件授权。

替换一个 hardlink 目录项不会改变旧 inode 的其他名称。POSIX 不再在启动前遍历可写目录树检查 hardlink，其路径规则不能保证阻止 Shell 通过预存 hardlink 别名写入其他位置。Windows 继续保留递归 ACL 和共享对象身份检查，可能在 Helper 启动前拒绝存在越界别名的授权。

Windows 执行器初始化时捕获直连 DOS 盘符到物理卷的映射，保存在受保护的运行时副本中，不涉及模型提供的路径。Helper 对 DOS 路径进行词法规范化，使用 `OBJ_DONT_REPARSE` 打开对应的完整原生路径；每个句柄仍检查 NTFS 卷和文件身份，并保留已验证的父目录用于子项打开和修改。文件操作拒绝文件系统重解析点，不依赖 LPAC 中不可用的宿主 DOS 设备命名空间查询、盘符根目录访问或严格最终路径规范化。原子替换使用 `FileRenameInformationEx`。临时文件继承目标父目录的 ACL，包括当前有效的执行授权；不复制旧文件的显式文件级 DACL，也不取得 Broker 的 `WRITE_DAC` 权限。依赖自定义文件 ACL 的程序需要考虑这一替换语义。运行时和控制文件路径仍由原生后端保护。

## 私有输出

所有模式均将命令和超长工具输出保存到 Virgo 用户目录。`context.outputs.create(prefix)` 返回已注册的 `OutputRecord` 及对应输出流。子进程只获得管道，只有 Broker 打开输出记录。内部 `CommandOutput.record` 保留该输出权限引用。输出不再写入项目 `.virgo/outputs`，文件工具也没有读取私有输出文件的特殊豁免。

结果返回随机 `output_id`。`read_output` 接收该 ID，以及按字节计算的 `offset`、`limit`。ID 必须对应来源会话注册的确切记录；任意路径、伪造记录、另一会话的 ID 均被拒绝。切换选中会话不会改变子任务所属的输出权限。

上述边界覆盖内置本地工具。本地 MCP stdio Server 通过维护中的适配器使用同一执行器。Python 适配器本身、明确受信任的宿主扩展和 hooks 保留宿主权限；远程 MCP 需要独立的外部能力适配器，不受本地操作系统沙箱保护。

## 验证与测试迁移

`tests/tools/test_fs.py` 保留已有编辑和搜索用例，改为启动真实 Helper 进程。路径权限用例使用真实 `SandboxExecutor`；后端不可用是失败，不以模拟执行算作隔离通过。

POSIX 与 Windows 原生测试复用同一个文件验收场景：执行全部五种文件工具，直接调用执行器检查 Read-Only 的操作系统拒绝，验证控制文件和运行时副本，以及私有输出 ID 的来源限制。已经获得工作区写权限并开始运行的进程，还会尝试修改后续执行器的副本和共享运行时父目录下新建的普通测试文件；普通工作区写入则必须成功。发布前必须在实际平台执行；仅有本地 Host 进程测试不足以证明原生隔离。

旧 `test_core_file_mutation_survives_hardlink_unlink_race[write/edit]` 通过跟踪已移除的 Broker 修改函数暂停执行，因此退役。替代用例 `test_core_file_mutation_replaces_hardlink_without_touching_source[write/edit]` 启动真实 No-Limit Helper，要求提交新 inode 且不改变外部源文件。原生 Helper hardlink 和路径替换测试继续保留。没有退役输出或文件算法测试。

统一权限策略迁移退役以下旧边界断言：

- `test_native_rejects_preexisting_writable_hardlinks[outside/metadata]` 依赖已移除的 POSIX 全树扫描；工作区内 hardlink 的正向用例保留。`test_sandbox_file_tools_reject_hardlinks` 在 POSIX 上验证实际 Helper 替换后源文件不变，在 Windows 上保留必要的启动前拒绝。
- `test_grant_cannot_be_reused_after_context_changes[resource]` 将批准绑定到 Broker 探测的 inode。替代用例 `test_resource_replacement_keeps_the_lexical_operation_grant` 验证：确切操作不变时仍持有获批路径范围，最终对象由原生边界解析和约束。
- `test_direct_file_approval_dispatches_once_and_preserves_the_mode` 依赖自动推断审批。替代用例 `test_file_approval_requires_explicit_request_and_preserves_the_mode` 验证拒绝、显式申请、仅一次成功执行和基础策略恢复。
