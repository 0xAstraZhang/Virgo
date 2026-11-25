# 006 — 三层渐进式、与服务商无关的上下文压缩

[决策索引](README.md) · [文档中心](../README.md) · [English](../../en/decisions/006-three-layer-provider-agnostic-compaction.md)

**状态：** 已采纳

## 背景

同时维护 Anthropic、OpenAI Responses、Codex 三条原生压缩路径和一条通用摘要路径带来了严重的行为割裂：不透明加密块导致切换模型/服务商时必须回滚完整原始历史；OpenAI/Codex 端点不支持 `/compact [focus]` 与短预算降级重试；此外，上下文在达到 80% 阈值前完全不做清理，一旦触发就直接执行高成本全量摘要。

## 决策

- **彻底移除服务商原生压缩**：删除 `virgo.ai.compaction` 与各适配器的 `compact()` 实现（`anthropic`、`responses`、`codex`），移除 `restore_native_context` 与 `preserve_thinking`。包含旧版原生压缩 `context` 字段的会话文件在加载时显式报错并提示开启新会话。
- **由 `CompactionController` 统一驱动三层渐进式压缩**（`src/virgo/harness/compaction.py`）：
  - **L0（`tools/output.py`）**：单次超限工具输出（>50 KB / 2000 行）落盘至 `OutputStore`，上下文仅保留预览与 `output_id`。
  - **L1（批量微压缩）**：普通轮次保持严格追加（append-only）以保护 Prompt Cache 前缀；当上下文占用达到 `compaction.pruneRatio`（默认 `0.6`）或触发 L2 前，对保护区之外的大体积旧 `ToolResultMessage` 进行一次批量折叠，替换为保留工具名、路径/命令/模式/URL、`output_id`、退出码与体积的语义存根，并要求回收量达到最小阈值以避免为微小收益破坏缓存。L1 以轻量 `mode="prune"` 记录持久化至 `SessionStore`，保证会话恢复后与内存视图完全一致且不改写原始历史。
  - **L2（结构化全量摘要）**：达到 `compaction.triggerRatio`（默认 `0.8`）或手动 `/compact [focus]` 时，按固定四段结构（`Goal & Constraints`、`Completed Work & Decisions`、`Current State & Open Issues`、`Next Steps`）生成摘要，并由 harness 确定性附加 `<read-files>` / `<modified-files>`。主动压缩默认在当前主模型上执行缓存复用（Cache-Warm In-Context）摘要（复用 `system + tools + messages[:cut]` 前缀），遇到 `overflow` 或模型误发 `tool_use` 时自动降级为离线序列化摘要。
  - **L3（溢出降级恢复）**：服务商返回上下文溢出错误时，先执行激进 L1 剪枝；若已低于窗口上限则直接重试而无需调用 LLM 摘要；否则执行离线序列化 L2（遇到 `length` 截断时支持一次短预算重试）。
- **区分 ASCII 与非 ASCII 的 Token 估算**：在缺少有效 `usage` 时，按 ASCII 约 4 字符/token、非 ASCII/CJK 约 1.5 字符/token 估算。

## 取舍与约束

- **不为旧版原生压缩会话提供静默迁移**：加载含旧版原生 `context` 字段的 JSONL 时直接报错，避免静默丢失状态。
- **批量剪枝而非逐轮滑动窗口**：L1 仅在跨阈值时批量触发，确保相邻轮次保持前缀不变以维持高缓存命中率。

## 相关文档

- [架构概览](../development/architecture.md)
- [005 — 运行时分层、扩展机制边界与本地持久化存储](005-runtime-layering-and-durable-state.md)
