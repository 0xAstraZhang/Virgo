# 006 — Three-layer provider-agnostic context compaction

[Decisions](README.md) · [Documentation](../README.md) · [简体中文](../../zh-CN/decisions/006-three-layer-provider-agnostic-compaction.md)

**Status:** Accepted

## Context

Maintaining separate native compaction paths for Anthropic, OpenAI Responses, and Codex alongside a portable summary path created significant architectural divergence: opaque encrypted blocks prevented cross-provider model switches without restoring full history, OpenAI/Codex endpoints ignored `/compact [focus]` and concise recovery budgets, and the runtime jumped directly from zero pruning to an expensive full summarization at 80% window usage.

## Decision

- **Remove provider-native compaction completely**: Retire `virgo.ai.compaction` and provider-specific `compact()` endpoints (`anthropic`, `responses`, `codex`), `restore_native_context`, and `preserve_thinking`. Legacy session files containing native compaction `context` records are rejected on load with an explicit error prompting a new session.
- **Three-layer progressive pipeline owned by `CompactionController`** (`src/virgo/harness/compaction.py`):
  - **L0 (`tools/output.py`)**: Per-tool output budgets spill oversized outputs (>50 KB / 2000 lines) to `OutputStore` with opaque `output_id`s.
  - **L1 (Batch Micro-Compaction)**: Normal turns remain strictly append-only to preserve prompt-cache prefixes. When usage reaches `compaction.pruneRatio` (default `0.6`) or before triggering L2, stale bulky `ToolResultMessage` payloads outside the protected tail are batch-collapsed into compact semantic stubs (retaining tool name, target path/command/pattern/URL, `output_id`, exit status, and size) only when the reclaimed token count meets a minimum cache-amortization threshold. L1 prunes are persisted as lightweight `mode="prune"` compaction entries in `SessionStore` so resume reproduces the exact live context without mutating original history.
  - **L2 (Structured Full Compaction)**: At `compaction.triggerRatio` (default `0.8`) or manual `/compact [focus]`, generate a 4-section structured summary (`Goal & Constraints`, `Completed Work & Decisions`, `Current State & Open Issues`, `Next Steps`) plus deterministic `<read-files>` / `<modified-files>` lists. Proactive runs default to cache-warm in-context summarization on the active model (reusing `system + tools + messages[:cut]`), automatically falling back to out-of-band serialized summarization on `overflow` or unexpected `tool_use`.
  - **L3 (Reactive Overflow Recovery)**: On provider context-overflow errors, first run aggressive L1 pruning; if usage drops below the context window, retry immediately without paying for an LLM summary; otherwise run serialized L2 (with optional concise retry on `length`).
- **ASCII / non-ASCII weighted token estimation**: When no post-compaction provider usage is available, estimate ASCII at ~4 chars/token and non-ASCII/CJK at ~1.5 chars/token.

## Tradeoffs and constraints

- **No silent migration for legacy native compaction sessions**: Existing JSONL files with native `context` blocks fail loudly on load rather than silently dropping provider-owned state.
- **Batched vs. per-turn pruning**: L1 deliberately avoids per-turn sliding-window mutation so prompt caching stays hot between batch prunes.

## Related

- [Architecture overview](../development/architecture.md)
- [005 — Runtime layering, extensibility boundaries, and durable state](005-runtime-layering-and-durable-state.md)
