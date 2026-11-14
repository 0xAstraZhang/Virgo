# Reviewed test retirements

[Development and CI](testing-and-ci.md) · [Comparison policy](testing-and-ci.md#reviewed-duplicate-retirements) · [Abstraction boundaries](abstraction-boundaries.md) · [简体中文](../../zh-CN/development/test-retirements.md)

## Core/plugin product retirements

Reviewed baseline: `47e1757bda5c5cfafb3a5a508a1a6ec4d629bfaf`. This migration retires **46 parameter-expanded cases** that exercise the six removed official products. They are recorded separately from duplicate cleanup: the old behavior is deliberately unavailable, so preserved coding tests are not described as equivalent coverage for a scheduler or team platform.

The exact baseline, original modules, full test identities and parameter IDs are maintained in [`core_plugin_retirements.json`](../../../ci/pipeline/core_plugin_retirements.json). The reasons and data locations are in [Core/plugin boundaries](core-plugin-boundaries.md) and [Migration](../guides/migrating-to-core-plugins.md).

| Retired product | Cases | Scope of retired behavior |
| --- | ---: | --- |
| Schedule | 7 | Cron parsing, scheduler notifications, time/file repair, ownership and two TUI multi-conversation scheduler parameters |
| Workflow | 3 | Python orchestration, journal/resume, workflow tool steps and unsafe run-ID rejection in the removed engine |
| Team | 8 | Long-lived workers, mailboxes, plan approval, worktrees, task cleanup and teammate-specific async/UI lifecycle |
| Tasks | 3 | Persistent dependency graph, task tools and task-file path handling |
| Memory | 11 | Save/index/tidy/update behavior plus eight tidy/extraction model-effort parameters |
| Goal | 14 | Evaluator input/verdicts, continuation loop, cancellation/limits plus four auxiliary model-effort parameters |

Only the six product test modules and their explicitly listed shared cases/parameters are retired. Existing auxiliary permission and compaction parameters remain. Background jobs, subagents, permissions, recovery, session ownership, native protocol and TUI interaction scenarios continue to run. The command-hint scenario now uses a distinct extension command because name collisions are rejected; it retains the completion/template/clipboard/history checks.

Before production code changed, `tests/harness/test_core_plugins_e2e.py` added eight assembled-session acceptance scenarios. The new contracts are exercised across the model/tool boundary, including file editing and a real subprocess, rather than by mirroring implementation methods.

CI permits these product retirements only when all of the following hold:

- The comparison baseline is exactly the reviewed SHA, the original module/identity matches, and each removed original case passed.
- Five exact preservation gates pass in both base and head: persistence failure, fail-closed policy order, background notification, subagent isolation and execution-time permission rechecks.
- All eight exact new Core/plugin acceptance cases pass in head, including preserved legacy files/history and the zero-plugin coding loop.
- The retired identity is absent from head. A still-present failing/skipped case receives ordinary regression checks; unrelated deletions receive no exemption.

Every case remains visible in `removed`; `retired_products` separately records the product decision and gates in readable and JSON output. No directory, prefix or count-wide exception is used. A changed baseline requires a fresh explicit review. The comparison CLI's pre-implementation acceptance scenarios cover wrong baselines/modules, failed originals, missing/failed/skipped gates and unrelated removal.

## Previous duplicate cleanup

Reviewed baseline: `0ffc23707de6e87a579a46a8f027679c253b78dc`. Nine non-parameterized duplicate cases were removed, reducing product tests by 105 lines. Retained tests were neither rewritten nor renamed. Existing fuller scenarios already covered the same behavior; test size or layer was not a reason for removal.

## Removed cases and retained coverage

Names below are the complete function identities used by CI; HTTP entries include parameter IDs. Exact original modules are recorded in [`ci/pipeline/reports.py::APPROVED_RETIREMENTS`](../../../ci/pipeline/reports.py).

| Removed case | Retained scenarios | Rationale |
| --- | --- | --- |
| `test_tool_calls_and_results_are_paired_in_order` | `test_parallel_results_are_emitted_as_completed_but_history_keeps_call_order`; `test_tool_completion_order_survives_reload_without_changing_model_history` | Forces out-of-order tool completion and checks live events, message history and ordering after disk reload. |
| `test_after_tool_call_overrides_result` | `test_post_tool_hook_feedback_keeps_image_results`; `test_post_tool_hook_feedback_keeps_terminate`; `test_hooks_cannot_erase_or_override_execution_and_adapter_facts` | Checks actual result replacement while protecting images, termination flags and execution/transport facts. |
| `test_result_hook_failure_preserves_the_original_tool_failure` | `test_projection_sees_final_hooks_and_recorded_evidence_once_in_provider_content` | Fails both the tool and result hook, then also checks original evidence and final provider-request content. |
| `test_result_hook_recovery_keeps_original_diagnostic_without_marking_success_failed` | `test_tool_failure_and_recovered_result_hook_keep_original_evidence` | Covers a hook recovering an error into success, with additional log, Runtime and tool correlation checks. |
| `test_retry_on_retryable_errors` | `test_recovered_retry_exposes_its_log_reference_in_text_and_json`; `test_retry_failure_survives_success_and_reload_without_prompt_contents` | Checks retry events, final answers, UI/JSON references and recovered evidence; the old case additionally waited through real backoff. |
| `test_system_prompt_includes_context_files` | `test_user_system_and_project_context_survive_for_their_roles`; `test_default_request_keeps_tool_definitions_without_duplicate_or_empty_sections` | Covers user/project instructions, roles and actual request prefixes beyond a single `AGENTS.md` substring. |
| `test_model_errors_are_reported` | `test_final_model_error_is_written_once_and_exposed_with_its_log_path`; `test_provider_error_ends_run_without_crash` | Covers failed completion, messages, log deduplication, UI paths and persistence recovery. |
| `test_codex_http_failure_redacts_access_token` | `test_http_failure_has_correlation_without_response_payload[429-virgo.ai.providers.codex-openai-codex]` | The shared HTTP matrix checks 429 classification, whole-message redaction, request correlation and response-body isolation. |
| `test_responses_http_error_is_classified` | `test_http_failure_has_correlation_without_response_payload[429-virgo.ai.providers.responses-openai-responses]` | The same matrix covers Responses 429 classification, request IDs, Retry-After and redaction. |

Tool/hook scenarios live under `tests/agent/` and `tests/harness/`; request/prompt and recovery scenarios under `tests/harness/`; the HTTP matrix is in `tests/ai/providers/test_error_diagnostics.py`.

## CI constraints

CI recognizes a reviewed retirement only for the original baseline SHA, module and exact identity, when the removed case passed and all listed retained scenarios passed in both base and head. Deletions remain in `removed`; `retired` names their retained coverage. There is no directory, prefix or count-based exemption.

A failing original case, missing/skipped/failing retained coverage, unrelated deletion, different baseline, unrelated same-name module, or an old case still present but failing/skipped continues to block approval. The acceptance scenarios were added to `ci/checks/comparison.py` and confirmed failing against the old comparator before its implementation changed. They use real temporary pytest projects and the comparison CLI.

Other permission, security, cancellation, malformed-data, protocol, concurrency and TUI scenarios remain. The product refactoring uses existing coverage without adding tests that mirror the extracted implementation.
