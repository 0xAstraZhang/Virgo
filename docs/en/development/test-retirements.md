# Reviewed test retirements

[Development and CI](testing-and-ci.md) · [Comparison policy](testing-and-ci.md#reviewed-duplicate-retirements) · [Abstraction boundaries](abstraction-boundaries.md) · [简体中文](../../zh-CN/development/test-retirements.md)

## Execution-policy retirements

Reviewed baseline: `45bb6d52cffa4726e31329ba8f9c421b7b81d9a7`. This migration retires **42 parameter-expanded cases** for model-based command approval and the `accept-all` bypass. The exact original modules, full identities and parameter IDs are in [`execution_policy_retirements.json`](../../../ci/pipeline/execution_policy_retirements.json). The replacement contract is described in [Execution-policy migration](../guides/migrating-execution-policy.md).

These are deliberately removed contracts, not redundant security tests. There is no auxiliary model verdict, serialized approval-evidence prompt, verdict risk/authorization protocol, or permission small-model request to exercise. Revocation, source ownership, execution-time rechecks and denial priority remain necessary and retain behavioral coverage under deterministic policy. The four compaction model-effort parameters and shared completion-limit tests remain.

| Original module | Cases | Retired contract |
| --- | ---: | --- |
| `tests.features.test_permission` | 9 | Eight model-review execution/fallback/cancellation cases and one `accept-all` bypass case |
| `tests.features.test_permission_context` | 14 | Review-input serialization, evidence budgeting, history ordering/truncation and reviewer snapshots |
| `tests.features.test_permission_verdicts` | 15 | Model verdict validation, risk/authorization evidence, request limits and in-flight reviewer decisions |
| `tests.harness.test_small_model_calls` | 4 | Only the `consumer=permission` model-effort parameters |

Eight exact preservation gates must pass in **both base and head**:

| Retained boundary | Required identity |
| --- | --- |
| Source remains bound across chat switches | `tests.features.test_permission_context::test_direct_main_event_binds_once_across_chat_switch` |
| Unbound work cannot borrow main authorization | `tests.features.test_permission_context::test_unbound_nonmain_event_cannot_borrow_main_authorization` |
| Background children capture their original source | `tests.features.test_permission_context::test_background_subagent_captures_origin_before_worker_starts` |
| Execution rechecks current policy, arguments and symlinks | `tests.features.test_permission::test_execution_rechecks_mode_arguments_and_symlink_boundary` |
| New restrictions invalidate remembered approval | `tests.features.test_permission_verdicts::test_new_user_restriction_invalidates_session_approval` |
| Permission-hook errors fail closed | `tests.features.test_permission_verdicts::test_permission_hook_errors_cannot_fall_through_event_bus` |
| Background work still notifies the owning session | `tests.features.test_background::test_background_job_notifies_idle_session` |
| Children share budget, permission ceiling and cancellation | `tests.harness.test_core_plugins_e2e::test_children_share_budget_permission_ceiling_and_cancellation` |

All **nine parameter-expanded acceptance cases** in `tests.features.test_permission_policy_e2e` must also pass in head, using their exact manifest identities. They verify that project settings cannot grant authority, remove user denies or declare mutating commands read-only; `never` denies unknown operations without a prompt; hard sandbox denial cannot be approved; and changing requested configuration cannot mislabel host execution as isolated. These new cases are not required on the old baseline.

The comparator selects exactly one manifest for the exact base SHA. It does not merge this inventory with the historical 46-case policy below, and a missing or ambiguous policy grants no retirement exception. Every removed original must have passed in its original module. Each identity stays in `removed`, while `retired_products` records its explicit retirement category; the Markdown report shows the matching reason and gates. An identity still present in head cannot use this exception to hide a failure or new skip. No directory, name prefix, parameter prefix or count-wide exemption exists.

Before the comparator implementation changed, its new real-CLI acceptance generated 50 passing baseline cases (42 originals and eight preservation gates) and 17 passing head cases (eight preservation and nine acceptance gates). The old comparator correctly rejected the previously unrecognized policy with `42 previously executed tests disappeared`. The completed checks exercise the full inventory and reject wrong baselines, original modules and parameter IDs; failed originals; each missing gate; failed, skipped or relocated gates; unrelated deletion; and still-present failures/skips. The historical retirement checks and normal failure/skip/removal checks remain. Native sandbox validation and the rest of CI receive no exemption from this manifest.

## Removed checkout entry point

The 0.3 change removes the root `main.py` compatibility launcher. Six `tests.cli.test_main` cases lose their `[checkout]` parameter only; the `[command]` and `[module]` variants retain the same assertions in base and head. `ci/pipeline/reports.py::APPROVED_RETIREMENTS` records this baseline and the exact retained coverage, so the comparison reports a reviewed duplicate removal rather than six lost tests.

## Renamed identities

Three existing cases keep their behavior but changed identity in this change: the planning/read-only test name and the two localized `Work mode` parameter labels. `ci/pipeline/reports.py::PARAMETER_ALIASES` maps each old identity to its new name, including the JUnit-escaped Chinese parameter ID. The alias is applied to both base and head reports, so the comparison sees a rename rather than a lost test.

## Shell-scope classifier retirement

Reviewed baseline: `1b8e14b9be519adc02d9372aca9c08be9a55262f`. This change retires **112 parameter-expanded cases** from `tests/features/test_permission_scope.py`. The removed classifier (`is_simple_read_command`, `command_boundary`, `command_approval_reason`) had no production caller after managed operations became the only approval boundary; the surviving contract is the exact path-target binding in `command_path_targets`.

The exact identities are in [`shell_scope_retirements.json`](../../../ci/pipeline/shell_scope_retirements.json). Four preservation gates pass in base and head: approved shell path targets, dynamic-target reapproval, execution-time argument/symlink rechecks, and persisted preset values. Head-only acceptance covers the live path-target binder, the canonical permission axes and the captured permission snapshot.

## Previous Core/plugin product retirements

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

Only the six product test modules and their explicitly listed shared cases/parameters were retired by this policy. It retained the auxiliary permission and compaction parameters; the later execution-policy migration above retires only the permission parameters. Background jobs, subagents, permissions, recovery, session ownership, native protocol and TUI interaction scenarios continue to run. The command-hint scenario uses a distinct extension command because name collisions are rejected; it retains the completion/template/clipboard/history checks.

Before production code changed, `tests/harness/test_core_plugins_e2e.py` added eight assembled-session acceptance scenarios. The new contracts are exercised across the model/tool boundary, including file editing and a real subprocess, rather than by mirroring implementation methods.

CI permits these product retirements only when all of the following hold:

- The comparison baseline is exactly the reviewed SHA, the original module/identity matches, and each removed original case passed.
- Five exact preservation gates pass in both base and head: persistence failure, fail-closed policy order, background notification, subagent isolation and execution-time permission rechecks.
- All eight exact new Core/plugin acceptance cases pass in head, including preserved legacy files/history and the zero-plugin coding loop.
- The retired identity is absent from head. A still-present failing/skipped case receives ordinary regression checks; unrelated deletions receive no exemption.

Every case remains visible in `removed`; `retired_products` records the product decision in JSON, and the readable report also lists the matching policy's gates. No directory, prefix or count-wide exception is used. A changed baseline requires a fresh explicit review. The comparison CLI's pre-implementation acceptance scenarios cover wrong baselines/modules, failed originals, missing/failed/skipped gates and unrelated removal. This historical manifest is unchanged by the execution-policy migration.

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

For the nine duplicate retirements above, CI recognizes a reviewed retirement only for the original baseline SHA, module and exact identity, when the removed case passed and all listed retained scenarios passed in both base and head. Deletions remain in `removed`; `retired` names their retained coverage. There is no directory, prefix or count-based exemption.

A failing original case, missing/skipped/failing retained coverage, unrelated deletion, different baseline, unrelated same-name module, or an old case still present but failing/skipped continues to block approval. The acceptance scenarios were added to `ci/checks/comparison.py` and confirmed failing against the old comparator before its implementation changed. They use real temporary pytest projects and the comparison CLI.

Other permission, security, cancellation, malformed-data, protocol, concurrency and TUI scenarios remain. The product refactoring uses existing coverage without adding tests that mirror the extracted implementation.
