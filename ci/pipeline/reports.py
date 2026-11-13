"""Report integrity rules and approved test identity aliases."""

from __future__ import annotations

import math
from collections import Counter

BAD = {"failed", "error"}
STATUSES = BAD | {"passed", "skipped"}
PARAMETER_ALIASES = {
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
