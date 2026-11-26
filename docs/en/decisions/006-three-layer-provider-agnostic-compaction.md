# 006 — Three-layer provider-agnostic context compaction

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/006-three-layer-provider-agnostic-compaction.md)

**Status:** Accepted

## Context

Maintaining separate native compaction paths for Anthropic, OpenAI Responses, and Codex alongside a portable summary path created significant architectural divergence: opaque encrypted blocks prevented cross-provider model switches without restoring full history, OpenAI/Codex endpoints ignored `/compact [focus]` and concise recovery budgets, and the runtime jumped directly from zero pruning to an expensive full summarization at 80% window usage.

## Decision

- **Portable summaries across providers.** Virgo removed provider-native compaction from Anthropic, OpenAI Responses, and Codex. Session files with legacy native compaction `context` records raise an error on load that prompts the user to start a new session.
- **Three-layer progressive pipeline owned by `CompactionController`** (`src/virgo/harness/compaction.py`):
  - **L0 (Tool output limits).** Tools using the shared output budget save text beyond 50 KiB or 2,000 lines to `OutputStore` and return a bounded preview with an `output_id`.
  - **L1 (Batch pruning).** At `compaction.pruneRatio` (default `0.6`), replace bulky old tool results outside the protected tail with short descriptions if the batch reclaims enough tokens. The descriptions retain available tool name, target, output ID, exit status, and size. `SessionStore` appends `mode="prune"` records. Rebuilding the context reapplies those records to retained results, including results kept by a later summary. When L2 is already due, Virgo applies the L1 plan only if projected usage falls below the effective L2 threshold. Otherwise L2 sees the intact tool output.
  - **L2 (Structured summaries).** At `compaction.triggerRatio` (default `0.8`) or on manual `/compact [focus]`, summarize older turns under `Goal & Constraints`, `Completed Work & Decisions`, `Current State & Open Issues`, and `Next Steps`. The prose target is one quarter of the estimated evicted input, including the previous summary when present, within the 256-token minimum and model-window cap. The harness adds deterministic `<read-files>` and `<modified-files>` lists. While estimated usage remains below the model context window, proactive automatic compaction preserves a trailing tool-call/result batch that the assistant has not yet seen (deferring when no earlier turns exist to evict), whereas over-window and L3 overflow compaction may still evict it. Automatic and manual compaction preview the current model's full prompt prefix (including hosted tool declarations) without binding the agent's execution routes, retry transient `retryable` errors within a bounded limit, and fall back to a separate tool-free serialized-transcript request when a configured web route is unavailable, on overflow, unexpected client/hosted tool execution, or exhausted retryable failures. Ordinary agent requests still report unavailable configured routes rather than silently changing them.
  - **L3 (Overflow recovery).** On context overflow, try aggressive pruning first. If pruning brings estimated usage below the window, retry the request. If pruning is insufficient or the pruned request also overflows, run L2 with a serialized transcript. A summary that hits its output limit can qualify for one shorter attempt.
- **ASCII / non-ASCII weighted token estimation**: When no post-compaction provider usage is available, estimate ASCII at ~4 chars/token and non-ASCII/CJK at ~1.5 chars/token.

## Tradeoffs and constraints

- **Prompt caching.** Ordinary turns append to the prompt between batches, allowing the provider to reuse its cached prefix.

## Related

- [Architecture overview](../development/architecture.md)
- [005 — Runtime layering, extensibility boundaries, and durable state](005-runtime-layering-and-durable-state.md)
