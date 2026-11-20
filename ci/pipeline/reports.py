"""Report integrity and test-name normalization (no SHA-bound retirement files)."""

from __future__ import annotations

import math
from collections import Counter

BAD = {"failed", "error"}
STATUSES = BAD | {"passed", "skipped"}
PARAMETER_ALIASES = {
    # Diagnostics now retain history; budgets apply to query pages, not retention.
    "test_diagnostic_log_rotation_is_bounded_and_only_touches_its_own_files":
        "test_diagnostic_log_history_is_complete_and_only_touches_its_own_files",
    "test_multilingual_record_obeys_escaped_json_byte_budget":
        "test_multilingual_record_preserves_causes_and_query_byte_budget",
    "test_rotation_updates_cached_record_locations_and_marks_expired_logs":
        "test_history_growth_keeps_record_locations_and_deduplicates_old_errors",
    "test_zero_backups_still_enforces_rotation_budget":
        "test_history_remains_queryable_after_connection_closes",
    "test_unusable_log_budget_is_rejected":
        "test_unusable_query_budget_is_rejected",
    "test_expired_unknown_and_uncaptured_evidence_are_distinct_without_path_fallback":
        "test_retained_unknown_and_uncaptured_evidence_are_distinct_without_path_fallback",
    "test_expired_pending_references_cannot_block_retained_evidence":
        "test_missing_pending_references_cannot_block_retained_evidence",
    "test_oversized_log_record_keeps_id_phase_and_cause_summary_within_budget[512]":
        "test_large_log_record_keeps_causes_with_bounded_query_pages[512]",
    "test_oversized_log_record_keeps_id_phase_and_cause_summary_within_budget[1000]":
        "test_large_log_record_keeps_causes_with_bounded_query_pages[1000]",
    "test_oversized_log_record_keeps_id_phase_and_cause_summary_within_budget[4096]":
        "test_large_log_record_keeps_causes_with_bounded_query_pages[4096]",
    # Catalog discovery now validates npm metadata without executing the CLI.
    "test_cli_version_probe_is_bounded_and_only_parses_codex_output[codex-cli 0.220.0\\n-0.220.0]":
        "test_cli_metadata_is_bounded_and_only_accepts_codex[stable]",
    "test_cli_version_probe_is_bounded_and_only_parses_codex_output[codex v1.2.3-1.2.3]":
        "test_cli_metadata_is_bounded_and_only_accepts_codex[major]",
    "test_cli_version_probe_is_bounded_and_only_parses_codex_output[codex-cli 0.220.0-alpha.1-None]":
        "test_cli_metadata_is_bounded_and_only_accepts_codex[prerelease]",
    "test_cli_version_probe_is_bounded_and_only_parses_codex_output[arbitrary 1.2.3-None]":
        "test_cli_metadata_is_bounded_and_only_accepts_codex[wrong-package]",
    "test_cli_probe_failure_is_optional": "test_cli_metadata_failure_is_optional",
    "test_compaction_does_not_enable_native_search[openai-responses]":
        "test_compaction_does_not_enable_native_search[virgo.ai.providers.responses]",
    "test_compaction_does_not_enable_native_search[openai-codex]":
        "test_compaction_does_not_enable_native_search[virgo.ai.providers.codex]",
    "test_subagent_request_exposes_only_its_own_tools":
        "test_subagent_system_prompt_lists_only_its_own_tools",
    "test_noninteractive_sessions_report_no_answer_without_a_prompt":
        "test_noninteractive_sessions_get_a_fallback_without_a_prompt",
    "test_runs_woken_by_notifications_time_out_without_an_answer":
        "test_runs_woken_by_notifications_time_out_to_the_recommendation",
    "test_mcp_refresh_preserves_runtime_local_tools":
        "test_runtime_local_tools_survive_refresh",
    "test_parallel_todos_have_no_size_cap_or_stop_continuation":
        "test_todo_validation_and_stop_reminder",
    "test_unreadable_thoughts_are_hidden_live_and_in_history":
        "test_encrypted_thought_label_is_display_only_live_and_in_history",
    "test_inline_activity_is_separate_from_thought_folding_and_follows_prompt":
        "test_inline_activity_reuses_thought_header_and_keeps_prompt_before_reply",
    "test_catalog_refreshes_on_restart_and_keeps_cache_after_failure":
        "test_catalog_refreshes_on_each_open_and_does_not_fallback_after_failure",
    "test_picker_escape_does_not_cancel_background_startup_refresh":
        "test_escape_cancels_live_catalog_loading_without_opening_stale_picker",
    "test_configured_luna_reasoning_metadata_overrides_live_defaults[None]":
        "test_live_luna_reasoning_metadata_overrides_builtin_and_configured_defaults[None]",
    "test_configured_luna_reasoning_metadata_overrides_live_defaults[max]":
        "test_live_luna_reasoning_metadata_overrides_builtin_and_configured_defaults[max]",
    "test_only_header_click_toggles_immediately[header]":
        "test_single_click_anywhere_toggles_only_once[header]",
    "test_only_header_click_toggles_immediately[body]":
        "test_single_click_anywhere_toggles_only_once[body]",
    "test_only_header_click_toggles_immediately[padding]":
        "test_single_click_anywhere_toggles_only_once[padding]",
    "test_web_search_result_header_toggles_and_body_keeps_details_open":
        "test_web_search_result_body_click_collapses_only_result",
    "test_plan_mode_only_allows_reads_and_dominates_agent_override":
        "test_planning_task_does_not_restrict_writes_and_child_ceiling_still_applies",
    "test_work_mode_command_is_localized_and_available_without_plugins[en-Work mode]":
        "test_work_mode_command_is_localized_and_available_without_plugins[en-Plan task]",
    "test_work_mode_command_is_localized_and_available_without_plugins[zh-CN-工作模式]":
        "test_work_mode_command_is_localized_and_available_without_plugins[zh-CN-Plan 任务]",
    # JUnit escapes non-ASCII parameter IDs; keep the escaped identity too.
    "test_work_mode_command_is_localized_and_available_without_plugins[zh-CN-\\u5de5\\u4f5c\\u6a21\\u5f0f]":
        "test_work_mode_command_is_localized_and_available_without_plugins[zh-CN-Plan \\u4efb\\u52a1]",
}


def validate_report(data):
    if not isinstance(data, dict):
        raise ValueError("Report must be an object")
    for key in ("platform", "label", "sha"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Missing or invalid {key}")
    if data.get("complete") is not True or type(data.get("exit_code")) is not int or data["exit_code"] not in {0, 1}:
        raise ValueError("pytest did not complete normally")
    duration = data.get("duration")
    if type(duration) not in {float, int} or not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid duration")
    for key in ("problems", "collection_skips"):
        if not isinstance(data.get(key), list) or not all(isinstance(value, str) for value in data[key]):
            raise ValueError(f"Invalid {key}")
    if not isinstance(data.get("tests"), dict) or not data["tests"]:
        raise ValueError("No individual test outcomes in report")
    counts = Counter()
    for name, case in data["tests"].items():
        if not name or not isinstance(case, dict) or case.get("status") not in STATUSES:
            raise ValueError(f"Invalid test outcome: {name}")
        counts[case["status"]] += 1
    counts["skipped"] += len(data["collection_skips"])
    actual = data.get("counts")
    if (not isinstance(actual, dict) or any(key not in STATUSES or type(value) is not int or value < 0
                                          for key, value in actual.items()) or Counter(actual) != counts):
        raise ValueError("Test counts do not match individual outcomes")
    if bool(data["exit_code"]) != bool(counts["failed"] + counts["error"]):
        raise ValueError("pytest exit code does not match test outcomes")
