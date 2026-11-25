# 006 — 三层渐进式、与服务商无关的上下文压缩

[决策索引](README.md) · [文档中心](../README.md) · [English](../../en/decisions/006-three-layer-provider-agnostic-compaction.md)

**状态：** 已采纳

## 背景

同时维护 Anthropic、OpenAI Responses、Codex 三条原生压缩路径和一条通用摘要路径带来了严重的行为割裂：不透明加密块导致切换模型/服务商时必须回滚完整原始历史；OpenAI/Codex 端点不支持 `/compact [focus]` 与短预算降级重试；此外，上下文在达到 80% 阈值前完全不做清理，一旦触发就直接执行高成本全量摘要。

## 决策

- **摘要可跨服务商使用。** Virgo 已移除 Anthropic、OpenAI Responses 与 Codex 的原生压缩。含旧式原生压缩 `context` 记录的会话文件会在加载时报错，并提示开启新会话。
- **由 `CompactionController` 统一驱动三层渐进式压缩**（`src/virgo/harness/compaction.py`）：
  - **L0（工具输出限额）。** 对于采用共享输出限额的工具，输出超过 50 KiB 或 2,000 行时将全文存入 `OutputStore`，并返回受限预览和 `output_id`。
  - **L1（批量剪枝）。** 上下文占用达到 `compaction.pruneRatio`（默认 `0.6`）或即将自动生成摘要时，若预计能回收足够的 token，就将保留区以外的大段旧工具结果替换为简述，保留可用的工具名、目标、输出 ID、退出状态和体积。`SessionStore` 追加 `mode="prune"` 记录。重建上下文时会将这些记录应用于仍然保留的工具结果，包括之后被摘要保留的结果。
  - **L2（结构化摘要）。** 达到 `compaction.triggerRatio`（默认 `0.8`）或手动执行 `/compact [focus]` 时，按 `Goal & Constraints`、`Completed Work & Decisions`、`Current State & Open Issues` 和 `Next Steps` 四段结构总结早期轮次。会话运行时按固定规则附加 `<read-files>` 与 `<modified-files>`。自动及手动压缩都会尽量复用当前模型的提示词前缀。摘要请求遇到上下文溢出或意外工具调用时，改用单独的请求总结序列化会话记录。
  - **L3（溢出恢复）。** 上下文溢出时先尝试激进剪枝。若剪枝后估算用量低于窗口上限，则重试请求。若剪枝不足，或重试再次溢出，则使用序列化会话记录执行 L2。摘要触及输出上限后，符合条件时可再尝试一次更短的摘要。
- **区分 ASCII 与非 ASCII 的 Token 估算**：在缺少有效 `usage` 时，按 ASCII 约 4 字符/token、非 ASCII/CJK 约 1.5 字符/token 估算。

## 取舍与约束

- **提示词缓存。** 两次批量剪枝之间，普通轮次向提示词追加消息，便于服务商复用缓存前缀。

## 相关文档

- [架构概览](../development/architecture.md)
- [005 — 运行时分层、扩展机制边界与本地持久化存储](005-runtime-layering-and-durable-state.md)
