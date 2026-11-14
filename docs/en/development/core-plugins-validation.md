# Core/plugin validation — 2026-11-14

[Core boundaries](core-plugin-boundaries.md) · [Testing and CI](testing-and-ci.md) · [Raw startup samples](../../ci-results/core-plugins-startup-2026-11-14.json) · [简体中文](../../zh-CN/development/core-plugins-validation.md)

This report records the local comparison for the Core/plugin refactor. It separates deterministic product acceptance, startup observations and the live-model evaluation that has not been performed.

## Test evidence

| Revision | Local complete-suite result |
| --- | --- |
| Baseline `47e1757bda5c5cfafb3a5a508a1a6ec4d629bfaf` | 1,591 passed; 9 skipped |
| Refactor candidate | 1,555 passed, 9 skipped; no additional skips or failures |

The eight assembled-session acceptance scenarios in `tests/harness/test_core_plugins_e2e.py` were committed before production implementation. They cover the minimal coding loop with actual file operations and a subprocess, resume, execution-time permissions, dynamic registration snapshots, partial setup rollback and cleanup, project trust, child authority/cancellation/shared budget, old data preservation and setting precedence.

Existing protocol, permission, cancellation, persistence, diagnostics, MCP, background and TUI scenarios remain in the suite. The 46 removed product scenarios are explicitly listed in the [retirement manifest](../../../ci/pipeline/core_plugin_retirements.json); their original product behavior is not counted as retained functionality. CI requires five preservation gates in both revisions and all eight new acceptance gates in the candidate. Cross-platform GitHub Actions results remain the source for Windows/macOS acceptance; this local run alone does not establish them.

The final MCP generation and shared-compaction-budget changes were checked against 95 existing/acceptance scenarios; MCP's final source also passed its 40-scenario subset. An offline run with the existing MCP echo server paused a child between two calls from one response: after another runtime published generation 2, both child calls completed on generation 1, stale direct handles were rejected before dispatch, and the old connection closed after the batch. This checks actual transport lifetime rather than schema counts alone.

The managed local runner's injected SOCKS proxy required `socksio` in the temporary validation environment for SDK construction. That environment-only addition is not a project dependency change; `pyproject.toml` and `uv.lock` are unchanged.

## Startup sampling method

Each column below contains **five fresh-process samples** from the same Linux workspace with CPython 3.13.5 and the unchanged `uv.lock` environment. The filesystem/dependency cache was warm. [`ci/measure_startup.py`](../../../ci/measure_startup.py) constructs an `AgentSession` using the offline `faux` model, an empty temporary working directory and user home, `refresh_models=False`, `extensions=False`, and no MCP server configuration. No credentials or live provider calls are involved.

The baseline loads its pre-refactor default features. The other columns select the implementation in this PR with `minimal` or `coding`. These are construction samples, not full CLI time-to-first-paint or time-to-first-model-token measurements. All sample values and visible tool names are retained in the [raw JSON](../../ci-results/core-plugins-startup-2026-11-14.json).

## Observed values

Values are medians unless a row specifies a range. Each timing column is summarized independently; the median import and construction values need not sum to the median total.

| Metric | Baseline default | `minimal` | `coding` |
| --- | ---: | ---: | ---: |
| Import time, ms | 99.668 | 77.031 | 71.528 |
| Session construction, ms | 36.729 | 13.462 | 26.286 |
| Import + construction, ms | 136.398 | 91.227 | 97.750 |
| Total observed range, ms | 104.095–147.591 | 84.759–103.487 | 94.853–119.569 |
| Imported Python modules | 346 | 312 | 340 |
| Imported Virgo modules | 88 | 64 | 82 |
| Live Python threads after construction | 2 | 1 | 1 |
| Model-visible tools | 28 | 6 | 14 |
| Serialized tool-schema characters | 13,430 | 2,950 | 7,277 |
| Base system-prompt characters | 926 | 682 | 926 |
| Process maximum RSS, KiB on Linux | 27,284 | 24,968 | 26,504 |

`minimal` exposes `read`, `write`, `edit`, `glob`, `grep` and the selected `bash` tool in this environment. `coding` additionally exposes `diagnostic_get`, `todo_write`, `ask_user`, `agent`, `job_output`, `job_kill`, `web_search` and `web_fetch`. MCP management tools are absent without server configuration. The former scheduler thread is absent in both candidate profiles.

The small sample illustrates this configuration's footprint. It is not a general startup speed guarantee: process scheduling, caches, selected shell, installed packages, configured resources and real connections change the result. RSS is a process high-water mark rather than retained heap size, and its units differ on other operating systems. Character counts are not model token counts or billed usage. The unchanged `coding` base-prompt length does not establish identical provider-native request bytes; existing request/prefix scenarios cover those contracts.

## Reproduce

Use the same interpreter and locked dependencies for both checkouts. Run each candidate command in a fresh process five times:

```bash
uv run --no-sync python ci/measure_startup.py --profile minimal
uv run --no-sync python ci/measure_startup.py --profile coding
```

The measurement helper is new in the refactor. For the baseline, invoke that same helper and interpreter with `PYTHONPATH` pointing to a separate checkout of `47e1757`, and omit `--profile`. The script creates disposable homes/workspaces, so it does not need existing user data. Retain every output and compute the median independently for each numeric field. Do not compare a cold install against a warm environment or silently discard slower samples.

## Evaluation still required for model quality

No live-provider coding success rate, intervention rate, token usage, cost, or task-completion latency was measured here. A reproducible follow-up should fix repository snapshots, model/version and effort, prompts, acceptance criteria, permission settings and budgets; run the same coding tasks on baseline and candidate; then record success, tool failures/recovery, human intervention, model calls, elapsed time and cost.

The deterministic acceptance run establishes the exercised runtime behavior. Fewer tools, lines or startup milliseconds are not substitutes for live coding quality and do not support a claim that coding performance improved.
