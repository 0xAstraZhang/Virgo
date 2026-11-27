# Functional acceptance and impact evidence

[Documentation](../README.md) · [Requirements](functional-requirements.md) · [Axiomatic Design](axiomatic-design.md) · [简体中文](../../zh-CN/development/functional-acceptance.md)

This page maps leaf FRs to representative checks and records controlled cross-functional variations. A test reference identifies executable evidence, not a permanent claim that every platform or revision has passed. Record the tested commit, command, environment, and result when reporting a verification run.

## Evidence scope

- **Structural evidence:** AST import checks establish the static DSM properties described in [Axiomatic Design](axiomatic-design.md). They do not measure functional sensitivity.
- **Behavioral evidence:** Assembled-session tests exercise public tools, model-visible requests, stored history, and actual file outcomes with deterministic `faux` responses. They establish the tested scenario, not natural-language task success probabilities.
- **Native evidence:** Filesystem/network isolation and process cleanup require the host-specific suites in [execution acceptance](execution-acceptance.md). A Broker file approval test cannot establish native OS isolation.
- **Unknown:** An untested relationship remains unknown. An unlisted sparse-matrix entry is not an established zero. A passing scenario does not justify a full leaf FR–DP independence matrix.

For each impact claim, specify the changed DP, fixed inputs/conditions, affected FR, observed outcome, and test. “No effect” always has a stated variation range; it does not cover arbitrary replacement code that violates the contract.

## Representative acceptance map

The [catalog](functional-requirements.md) supplies the required outcomes. These are focused entry points into the existing suites, not exhaustive coverage claims.

| Leaf FRs | Test source and representative checks | What the evidence covers |
| --- | --- | --- |
| FR-A.1, FR-A.2 | [CLI](../../../tests/cli/test_main.py): `test_cli_loads_features_and_streams_json`; [console](../../../tests/cli/test_console.py): `test_print_mode_json_lines_stay_whole_across_threads` | CLI output contracts and complete concurrent JSON records; presentation-specific suites cover TUI behavior |
| FR-A.3, FR-A.4 | [REPL](../../../tests/cli/test_repl.py): `test_double_slash_input_is_steered_as_prompt_not_queued_as_command`; [permission modes](../../../tests/harness/test_permission_modes_e2e.py): `test_direct_file_approval_persists_in_session_and_preserves_the_mode` | Input routing and fixed-answer approval adapter substitution |
| FR-A.5 | [Approval presentation](../../../tests/cli/test_approval_prompts.py): `test_grants_precede_details_with_distinct_project`; [prompt assembly](../../../tests/harness/test_prompt.py): `test_response_language_follows_user_message_in_each_ui_locale` | Localized grant descriptions and user-directed response-language guidance |
| FR-A.6 | [Clarification](../../../tests/features/test_ask_user.py): `test_noninteractive_sessions_report_no_answer_without_a_prompt`, `test_aborted_question_has_no_answer` | Explicit no-answer/cancellation behavior; no fabricated decision |
| FR-B.1, FR-B.4 | [Provider streaming](../../../tests/ai/providers/test_streaming.py): `test_anthropic_streaming_roundtrip`, `test_openai_streaming_roundtrip`; [provider errors](../../../tests/ai/providers/test_error_diagnostics.py): `test_network_failure_retains_original_cause` | Stream normalization and classified transport failures on local protocol fixtures |
| FR-B.2, FR-B.3 | [Preferences](../../../tests/harness/test_preferences.py): `test_remote_custom_provider_cannot_borrow_openai_credentials`, `test_effort_choice_only_changes_effort` | Credential identity boundaries and model/effort selection behavior |
| FR-C.1 | [test_fs.py](../../../tests/tools/test_fs.py): `test_file_tools_enforce_read_scope`, `test_glob_and_grep` | Actual reads/searches and explicit scope failures |
| FR-C.2 | [test_fs.py](../../../tests/tools/test_fs.py): `test_edit_unique_multi_and_errors` | Intended edits and ambiguous-match failures |
| FR-C.3, FR-C.4 | [Output storage](../../../tests/harness/test_output_store.py): `test_read_output_survives_session_resume`, `test_read_output_search_tail_and_line_paging`; [execution acceptance](execution-acceptance.md) | Recoverable bounded output; command lifecycle according to the applicable execution suite |
| FR-C.6 | [test_search.py](../../../tests/features/test_search.py): `test_modes_offer_exactly_one_search_path`, `test_started_native_search_is_not_replayed_or_downgraded` | Search capability selection and started-work replay guard |
| FR-C.6, FR-C.7 | [test_web_capabilities.py](../../../tests/features/test_web_capabilities.py): `test_combination_fallback_is_cached_and_no_duplicate_tools`, `test_native_fetch_started_disables_model_retry` | Combined capability fallback and hosted-work replay guard |
| FR-C.6, FR-C.7 | [test_fetch.py](../../../tests/features/test_fetch.py): `test_shared_web_budget_spans_search_and_fetch_and_resets_on_user_request` | Shared Web-call cap and reset behavior |
| FR-C.7 | [test_fetch.py](../../../tests/features/test_fetch.py): `test_fetch_result_reaches_model_and_persisted_history` | Content retrieval and persistence |
| FR-D.1, FR-D.2, FR-D.3, FR-D.4, FR-D.5, FR-D.6 | [Permission modes](../../../tests/harness/test_permission_modes_e2e.py): `test_changed_arguments_cannot_consume_another_path_permission`, `test_deny_and_noninteractive_policy_prevent_escalation`; [plugin boundaries](../../../tests/harness/test_core_plugins_e2e.py): `test_project_extensions_require_trust_outside_the_project` | Broker admission, confirmation and project trust; native guarantees require separate host suites |
| FR-D.6 | [Shell trust](../../../tests/harness/test_sandbox_e2e.py): `test_untrusted_project_shell_is_stripped_in_no_limit_mode`; [web trust](../../../tests/harness/test_guardrails.py): `test_untrusted_project_cannot_hijack_web_api_key_env_or_backend` | Project trust protects the selected Shell executable and the web backends and API key sources |
| FR-D.4 | [test_guardrails.py](../../../tests/harness/test_guardrails.py): `test_high_risk_approved_once_executes_and_second_call_prompts_again` | One-time high-risk confirmation and refusal before dispatch |
| FR-D.5 | [test_guardrails.py](../../../tests/harness/test_guardrails.py): `test_sandbox_unavailable_offers_one_shot_no_limit_and_preserves_session_mode`, `test_execution_failed_never_offers_no_limit_fallback` | One-shot fallback, unchanged session mode and no fallback after execution failure; not native isolation |
| FR-E.1 | [test_loop.py](../../../tests/agent/test_loop.py): `test_parallel_results_are_emitted_as_completed_but_history_keeps_call_order` | Scheduling and logical result order |
| FR-E.2 | [test_loop.py](../../../tests/agent/test_loop.py): `test_steering_is_delivered_before_next_model_call`, `test_follow_up_and_stop_hook_continue` | Steering and follow-up at request boundaries |
| FR-E.3 | [test_loop.py](../../../tests/agent/test_loop.py): `test_abort_stops_streaming`, `test_parallel_interrupt_signals_workers_cancels_queue_and_joins` | Cancellation of streaming and queued/active parallel calls |
| FR-E.4 | [test_loop.py](../../../tests/agent/test_loop.py): `test_max_turns` | Turn limits; the shared-budget experiment below covers request limits |
| FR-E.5 | [test_recovery_lifecycle.py](../../../tests/harness/test_recovery_lifecycle.py): `test_provider_retry_after_uses_existing_attempts_and_wait_budget` | Bounded retries |
| FR-E.5, FR-H.1 | [Core plugins](../../../tests/harness/test_core_plugins_e2e.py): `test_request_fallback_cannot_replay_started_hosted_work` | An extension requesting fallback cannot replay failed hosted work that has started; before-start fallback still works |
| FR-E.6, FR-E.7 | [Subagents](../../../tests/features/test_subagent.py): `test_subagent_reuse_survives_session_resume`; [background work](../../../tests/features/test_background.py): `test_job_tools_cannot_borrow_another_conversations_resources`; [core contracts](../../../tests/harness/test_core_plugins_e2e.py): `test_children_share_budget_permission_ceiling_and_cancellation` | Isolated reusable child state, job ownership and inherited limits |
| FR-E.8 | [Todos](../../../tests/features/test_todo.py): `test_incomplete_todos_survive_compaction_and_resume`, `test_invalid_todos_leave_previous_state_intact` | Valid checklist state across compaction/resume and rejected updates |
| FR-F.1, FR-F.2 | [Sessions](../../../tests/harness/test_session.py): `test_session_persists_and_resumes`, `test_resume_modes_recover_torn_tail`; [recovery lifecycle](../../../tests/harness/test_recovery_lifecycle.py): `test_background_failure_targets_original_runtime_after_switch` | Durable recovery and asynchronous ownership despite UI session switching |
| FR-F.3 | [Prompt assembly](../../../tests/harness/test_prompt.py): `test_user_system_and_project_context_survive_for_their_roles`, `test_permission_switch_updates_requests_and_restores_the_original_prefix` | Applicable instructions and permission-dependent request prefixes |
| FR-F.4 | [Compaction](../../../tests/harness/test_compaction.py): `test_manual_compaction_keeps_recent_and_strips_thinking`, `test_cancelled_compaction_ends_run_without_retry`; [provider recovery](../../../tests/harness/test_compaction_protocols.py): `test_provider_overflow_recovery` | Structural/history integrity, cancellation and protocol overflow recovery; not arbitrary semantic retention |
| FR-F.5, FR-C.5 | [Skills](../../../tests/features/test_skills.py): `test_skills_listed_and_forced`; [MCP](../../../tests/features/test_mcp_extension.py): `test_mcp_extension_reports_unavailable_servers_without_stopping`, `test_mcp_each_large_block_uses_originating_subagent_store` | Instruction selection, external-service failure isolation and result ownership |
| FR-G.1 | [test_observability.py](../../../tests/harness/test_observability.py): `test_main_request_header_is_persisted_before_dispatch_on_change` | Recorded configuration before dispatch |
| FR-G.2 | [test_observability.py](../../../tests/harness/test_observability.py): `test_retry_usage_includes_failed_attempt_without_double_counting_final` | Retry usage counted once; not billing reconciliation |
| FR-G.2 | [test_store_usage.py](../../../tests/harness/test_store_usage.py): `test_total_usage_counts_only_new_entries_and_follows_branch_switches` | Branch usage aggregation |
| FR-G.3, FR-G.4 | [Diagnostics](../../../tests/harness/test_diagnostics.py): `test_redaction_covers_credentials_payloads_urls_and_exception_override`, `test_log_write_failure_keeps_original_diagnostic_on_stderr` | Redaction, persistence fallback and retention of the original failure |
| FR-G.5 | [Scoped access](../../../tests/features/test_diagnostic_access.py): `test_shared_accounting_store_does_not_grant_child_or_sibling_evidence`; [query contract](../../../tests/harness/test_diagnostic_query_contract.py): `test_renamed_query_reference_can_be_followed_by_the_model`, `test_unavailable_query_is_not_advertised` | Scope restrictions and query names selected from the runtime's exposed declared tools |
| FR-G.6 | [Recovery lifecycle](../../../tests/harness/test_recovery_lifecycle.py): `test_reported_fault_does_not_start_an_extra_model_turn`; [query contract](../../../tests/harness/test_diagnostic_query_contract.py): `test_pending_evidence_uses_the_declared_query_name`, `test_compaction_checkpoint_uses_the_declared_query_name` | Delivery at existing boundaries and usable new checkpoint references |
| FR-H.1 | [Core plugins](../../../tests/harness/test_core_plugins_e2e.py): `test_failed_plugin_rolls_back_and_cleanup_continues`; [extension diagnostics](../../../tests/harness/test_extension_diagnostics.py): `test_extension_import_failure_is_saved_and_startup_continues` | Failed registration/load isolation and continued core availability |

## Controlled functional effects

### Compaction and task progression share a resource budget

[`test_auto_compaction_and_task_share_request_budget`](../../../tests/harness/test_compaction.py) fixes the task and initial conversation, varies the compaction threshold (DP-F.4) and total model-call budget (DP-E.4), and observes actual requests and persisted checkpoints through `AgentSession`.

| Compaction threshold | Model-call budget | Requests dispatched | Observable result |
| --- | ---: | --- | --- |
| High: summary deferred | 2 | Task, task | Task completes without a summary |
| Low: summary due | 1 | Task | No summary checkpoint; budget exhaustion prevents summarization |
| Low: summary due | 2 | Task, summary | Checkpoint is saved; no budget remains for task continuation |
| Low: summary due | 3 | Task, summary, task | Checkpoint is saved and the task completes |

At the same total budget, changing DP-F.4 changes the calls available for task progression and whether this scripted interaction reaches its next ordinary request and requested answer (FR-E.1). Budget exhaustion remains an explicit terminal outcome, not a violation of the orchestration protocol. Changing DP-E.4 can prevent summarization (FR-F.4). Budget enforcement (FR-E.4) remains satisfied in every case. The shared budget is deliberate: assigning a separate unlimited summary budget would change the resource contract. These cases do not measure summary factual accuracy or success on arbitrary tasks.

### Approval behavior under input-adapter substitution

[`test_direct_file_approval_persists_in_session_and_preserves_the_mode`](../../../tests/harness/test_permission_modes_e2e.py) uses both the recording adapter and the real plain `TerminalUI` input path, with fixed `y` then `n` answers. Both permit the requested first write, reuse that authorization for a second write, reject an unrelated file, and retain the `read-only` mode.

This supports invariance of FR-D.3 under the tested DP-A.4 substitution while request, answer, policy, and workspace remain fixed. It does not show that removing confirmation capability is harmless, that all presentation variations are equivalent, or that native Shell boundaries have been tested.

### Diagnostic-query substitution

[`test_diagnostic_query_contract.py`](../../../tests/harness/test_diagnostic_query_contract.py) substitutes a declared query tool name and follows the projected reference through an actual model tool call. It also covers pending evidence, post-compaction checkpoints, background result lookup, runtime tool restrictions, and declaration rollback.

New diagnostic projections select the first currently exposed tool in the runtime's tool order whose name is declared for diagnostic queries. With no eligible tool, they retain the evidence reference without advertising an unavailable query. This contract preserves FR-G.5/FR-G.6 when the query capability is renamed within that scope. It does not rewrite earlier history or guarantee availability after an arbitrary third-party request preparer rewrites the final request context.

### Permission and trust settings

In [prompt assembly](../../../tests/harness/test_prompt.py), `test_permission_switch_updates_requests_and_restores_the_original_prefix` fixes the session and empty message list, changes the effective mode (DP-D.1) to read-only, and restores the original mode. The tool schemas remain equal while the request's write boundary changes (FR-F.3). This is required policy propagation, not evidence of native isolation.

In [core plugins](../../../tests/harness/test_core_plugins_e2e.py), `test_project_extensions_require_trust_outside_the_project` holds the project extension and its self-declared trust setting fixed, varying the explicit external trust choice (DP-D.6). Only the trusted case runs the extension's marker write (FR-H.1). Trust is an intentional condition for loading, not a failure of extensibility.

### Failure-domain boundaries

Optional reporting and authoritative history have different contracts. In [diagnostic tests](../../../tests/harness/test_diagnostics.py), `test_log_write_failure_keeps_original_diagnostic_on_stderr` and `test_failing_ui_does_not_destroy_original_error_or_recurse` preserve the original failure through a reporting fallback. In [recovery tests](../../../tests/harness/test_recovery_lifecycle.py), `test_model_diagnostics_feature_can_be_disabled_without_disabling_logs` verifies that disabling the query plugin preserves logging and Core's bounded model-facing evidence without advertising the unavailable tool.

In contrast, [`test_failed_save_stops_requests_and_keeps_history`](../../../tests/harness/test_harness_boundaries.py) verifies that authoritative session persistence failure stops further requests and preserves unsaved history. This is an intentional influence of DP-F.1 on FR-E.1, not a failed attempt to isolate optional diagnostics.

Other leaf-level relationships, including general model-quality changes under compression, arbitrary extension request mutation, and joint task-success probability, have no complete functional-independence evidence here. Their status remains **unverified**, not zero.

## Reproducing the focused checks

Use the locked development environment and existing pytest entry points:

```bash
uv sync --locked --dev
uv run --no-sync pytest -q tests/test_architecture_matrix.py
uv run --no-sync pytest -q tests/harness/test_compaction.py::test_auto_compaction_and_task_share_request_budget tests/harness/test_permission_modes_e2e.py::test_direct_file_approval_persists_in_session_and_preserves_the_mode tests/harness/test_diagnostic_query_contract.py
uv run --no-sync pytest -q tests/harness/test_diagnostics.py tests/harness/test_recovery_lifecycle.py tests/harness/test_harness_boundaries.py
```

Choose additional suites from the acceptance map for the changed behavior. Follow [execution acceptance](execution-acceptance.md) for native guarantees. Avoid treating a fixture pass, a skip, or an import graph as an empirical information-axiom score.
