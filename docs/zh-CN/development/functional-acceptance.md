# 功能验收与影响证据

[文档中心](../README.md) · [需求目录](functional-requirements.md) · [公理设计](axiomatic-design.md) · [English](../../en/development/functional-acceptance.md)

本页将叶级 FR 映射到代表性检查，并记录受控的跨功能变化。测试引用标识可执行证据，不代表所有平台或后续版本永久通过。报告一次验证时，应记录测试提交、命令、环境与结果。

## 证据范围

- **结构证据：** AST 导入检查证明[公理设计](axiomatic-design.md)中限定的静态 DSM 性质，不测量功能敏感性。
- **行为证据：** 装配会话测试通过公开工具、模型可见请求、持久历史与实际文件结果验证行为，使用确定性的 `faux` 响应。它们证明所测场景，不估计自然语言任务的成功概率。
- **原生证据：** 文件系统/网络隔离与进程清理需要[执行验收](execution-acceptance.md)中的主机专用套件。Broker 文件审批测试不能证明原生 OS 隔离。
- **未知：** 未测试的关系仍是未知。稀疏矩阵未列出的条目不是已证实的零；单个场景通过不足以声明整张叶级 FR–DP 矩阵独立。

每项影响结论应说明调整的 DP、固定输入/条件、受影响 FR、观察结果与测试。“无影响”必须限定变化范围，不覆盖违反契约的任意替换代码。

## 代表性验收映射

[需求目录](functional-requirements.md)定义要求的结果。以下是现有套件的重点入口，不宣称穷尽覆盖。

| 叶级 FR | 测试来源与代表用例 | 证据覆盖范围 |
| --- | --- | --- |
| FR-A.1、FR-A.2 | [CLI](../../../tests/cli/test_main.py)：`test_cli_loads_features_and_streams_json`；[控制台](../../../tests/cli/test_console.py)：`test_print_mode_json_lines_stay_whole_across_threads` | CLI 输出契约与完整并发 JSON 记录；TUI 行为由呈现专用套件覆盖 |
| FR-A.3、FR-A.4 | [REPL](../../../tests/cli/test_repl.py)：`test_double_slash_input_is_steered_as_prompt_not_queued_as_command`；[权限模式](../../../tests/harness/test_permission_modes_e2e.py)：`test_direct_file_approval_persists_in_session_and_preserves_the_mode` | 输入路由，以及固定回答下的审批适配器替换 |
| FR-A.5 | [审批呈现](../../../tests/cli/test_approval_prompts.py)：`test_grants_precede_details_with_distinct_project`；[提示组装](../../../tests/harness/test_prompt.py)：`test_response_language_follows_user_message_in_each_ui_locale` | 本地化授权描述与遵循用户输入的回答语言指引 |
| FR-A.6 | [澄清](../../../tests/features/test_ask_user.py)：`test_noninteractive_sessions_report_no_answer_without_a_prompt`、`test_aborted_question_has_no_answer` | 显式未回答/取消行为，不虚构决定 |
| FR-B.1、FR-B.4 | [提供商流](../../../tests/ai/providers/test_streaming.py)：`test_anthropic_streaming_roundtrip`、`test_openai_streaming_roundtrip`；[提供商错误](../../../tests/ai/providers/test_error_diagnostics.py)：`test_network_failure_retains_original_cause` | 本地协议夹具下的流规范化与传输失败分类 |
| FR-B.2、FR-B.3 | [偏好设置](../../../tests/harness/test_preferences.py)：`test_remote_custom_provider_cannot_borrow_openai_credentials`、`test_effort_choice_only_changes_effort` | 凭据身份边界与模型/强度选择行为 |
| FR-C.1 | [test_fs.py](../../../tests/tools/test_fs.py)：`test_file_tools_enforce_read_scope`、`test_glob_and_grep` | 实际读取/检索与显式作用域失败 |
| FR-C.2 | [test_fs.py](../../../tests/tools/test_fs.py)：`test_edit_unique_multi_and_errors` | 目标编辑与歧义匹配失败 |
| FR-C.3、FR-C.4 | [输出存储](../../../tests/harness/test_output_store.py)：`test_ver08_read_output_survives_session_resume`、`test_read_output_search_tail_and_line_paging`；[执行验收](execution-acceptance.md) | 可恢复有界输出；命令生命周期由适用执行套件验证 |
| FR-C.6 | [test_search.py](../../../tests/features/test_search.py)：`test_modes_offer_exactly_one_search_path`、`test_started_native_search_is_not_replayed_or_downgraded` | 搜索能力选择及已启动工作的重放限制 |
| FR-C.6, FR-C.7 | [test_web_capabilities.py](../../../tests/features/test_web_capabilities.py)：`test_combination_fallback_is_cached_and_no_duplicate_tools`、`test_native_fetch_started_disables_model_retry` | 组合能力回退及托管工作重放限制 |
| FR-C.6, FR-C.7 | [test_fetch.py](../../../tests/features/test_fetch.py)：`test_shared_web_budget_spans_search_and_fetch_and_resets_on_user_request` | 共享 Web 调用上限及重置行为 |
| FR-C.7 | [test_fetch.py](../../../tests/features/test_fetch.py)：`test_fetch_result_reaches_model_and_persisted_history` | 内容获取与持久化 |
| FR-D.1, FR-D.2, FR-D.3, FR-D.4, FR-D.5, FR-D.6 | [权限模式](../../../tests/harness/test_permission_modes_e2e.py)：`test_changed_arguments_cannot_consume_another_path_permission`、`test_deny_and_noninteractive_policy_prevent_escalation`；[插件边界](../../../tests/harness/test_core_plugins_e2e.py)：`test_project_extensions_require_trust_outside_the_project` | Broker 准入、确认与项目信任；原生保证需单独主机套件 |
| FR-D.4 | [test_safety_v2.py](../../../tests/harness/test_safety_v2.py)：`test_ver10_high_risk_approved_once_executes_and_second_call_prompts_again` | 单次高危操作确认及分派前拒绝 |
| FR-D.5 | [test_safety_v2.py](../../../tests/harness/test_safety_v2.py)：`test_s502_sandbox_unavailable_offers_one_shot_no_limit_and_preserves_session_mode`、`test_s502_execution_failed_never_offers_no_limit_fallback` | 显式单次回退、会话模式不变及执行失败后禁止回退；不证明原生隔离 |
| FR-E.1 | [test_loop.py](../../../tests/agent/test_loop.py)：`test_parallel_results_are_emitted_as_completed_but_history_keeps_call_order` | 调度与逻辑结果顺序 |
| FR-E.2 | [test_loop.py](../../../tests/agent/test_loop.py)：`test_steering_is_delivered_before_next_model_call`、`test_follow_up_and_stop_hook_continue` | 请求边界上的转向及跟进 |
| FR-E.3 | [test_loop.py](../../../tests/agent/test_loop.py)：`test_abort_stops_streaming`、`test_parallel_interrupt_signals_workers_cancels_queue_and_joins` | 流及排队/活动并发调用的取消 |
| FR-E.4 | [test_loop.py](../../../tests/agent/test_loop.py)：`test_max_turns` | 轮次限制；下文共享预算实验覆盖请求上限 |
| FR-E.5 | [test_recovery_lifecycle.py](../../../tests/harness/test_recovery_lifecycle.py)：`test_provider_retry_after_uses_existing_attempts_and_wait_budget` | 有界重试 |
| FR-E.5、FR-H.1 | [核心插件](../../../tests/harness/test_core_plugins_e2e.py)：`test_request_fallback_cannot_replay_started_hosted_work` | 扩展请求回退不能重放已经启动的失败托管工作；启动前仍可正常回退 |
| FR-E.6、FR-E.7 | [子代理](../../../tests/features/test_subagent.py)：`test_subagent_reuse_survives_session_resume`；[后台工作](../../../tests/features/test_background.py)：`test_job_tools_cannot_borrow_another_conversations_resources`；[核心契约](../../../tests/harness/test_core_plugins_e2e.py)：`test_children_share_budget_permission_ceiling_and_cancellation` | 可隔离复用的子状态、任务归属及继承上限 |
| FR-E.8 | [Todos](../../../tests/features/test_todo.py)：`test_incomplete_todos_survive_compaction_and_resume`、`test_invalid_todos_leave_previous_state_intact` | 跨压缩/恢复的有效清单状态及被拒更新 |
| FR-F.1、FR-F.2 | [会话](../../../tests/harness/test_session.py)：`test_session_persists_and_resumes`、`test_resume_modes_recover_torn_tail`；[恢复生命周期](../../../tests/harness/test_recovery_lifecycle.py)：`test_background_failure_targets_original_runtime_after_switch` | 持久恢复，以及 UI 切换会话后的异步归属 |
| FR-F.3 | [提示组装](../../../tests/harness/test_prompt.py)：`test_user_system_and_project_context_survive_for_their_roles`、`test_permission_switch_updates_requests_and_restores_the_original_prefix` | 适用指令及受权限影响的请求前缀 |
| FR-F.4 | [压缩](../../../tests/harness/test_compaction.py)：`test_manual_compaction_keeps_recent_and_strips_thinking`、`test_cancelled_compaction_ends_run_without_retry`；[提供商恢复](../../../tests/harness/test_compaction_protocols.py)：`test_provider_overflow_recovery` | 结构/历史完整性、取消与协议超限恢复；不证明任意语义保留 |
| FR-F.5、FR-C.5 | [技能](../../../tests/features/test_skills.py)：`test_skills_listed_and_forced`；[MCP](../../../tests/features/test_mcp_extension.py)：`test_mcp_extension_reports_unavailable_servers_without_stopping`、`test_mcp_each_large_block_uses_originating_subagent_store` | 指令选择、外部服务失败隔离与结果归属 |
| FR-G.1 | [test_observability.py](../../../tests/harness/test_observability.py)：`test_main_request_header_is_persisted_before_dispatch_on_change` | 分派前记录有效配置 |
| FR-G.2 | [test_observability.py](../../../tests/harness/test_observability.py)：`test_retry_usage_includes_failed_attempt_without_double_counting_final` | 重试用量计入一次；不证明账单对账 |
| FR-G.2 | [test_store_usage.py](../../../tests/harness/test_store_usage.py)：`test_total_usage_counts_only_new_entries_and_follows_branch_switches` | 分支用量汇总 |
| FR-G.3、FR-G.4 | [诊断](../../../tests/harness/test_diagnostics.py)：`test_redaction_covers_credentials_payloads_urls_and_exception_override`、`test_log_write_failure_keeps_original_diagnostic_on_stderr` | 脱敏、持久化兜底及原故障保留 |
| FR-G.5 | [作用域访问](../../../tests/features/test_diagnostic_access.py)：`test_shared_accounting_store_does_not_grant_child_or_sibling_evidence`；[查询契约](../../../tests/harness/test_diagnostic_query_contract.py)：`test_renamed_query_reference_can_be_followed_by_the_model`、`test_unavailable_query_is_not_advertised` | 作用域限制，以及从运行时实际暴露且已声明工具中选择查询名称 |
| FR-G.6 | [恢复生命周期](../../../tests/harness/test_recovery_lifecycle.py)：`test_reported_fault_does_not_start_an_extra_model_turn`；[查询契约](../../../tests/harness/test_diagnostic_query_contract.py)：`test_pending_evidence_uses_the_declared_query_name`、`test_compaction_checkpoint_uses_the_declared_query_name` | 在既有边界投递，以及可用的新检查点引用 |
| FR-H.1 | [核心插件](../../../tests/harness/test_core_plugins_e2e.py)：`test_failed_plugin_rolls_back_and_cleanup_continues`；[扩展诊断](../../../tests/harness/test_extension_diagnostics.py)：`test_extension_import_failure_is_saved_and_startup_continues` | 注册/加载失败隔离与核心持续可用 |

## 受控功能影响

### 压缩与任务推进共用资源预算

[`test_auto_compaction_and_task_share_request_budget`](../../../tests/harness/test_compaction.py)固定任务和初始对话，改变压缩阈值（DP-F.4）及模型调用总预算（DP-E.4），经 `AgentSession` 观察实际请求和持久化检查点。

| 压缩阈值 | 模型调用预算 | 实际分派请求 | 可观察结果 |
| --- | ---: | --- | --- |
| 高：推迟摘要 | 2 | 任务、任务 | 任务完成，不生成摘要 |
| 低：需要摘要 | 1 | 任务 | 没有摘要检查点；预算耗尽阻止摘要 |
| 低：需要摘要 | 2 | 任务、摘要 | 检查点已保存；剩余预算不足以继续任务 |
| 低：需要摘要 | 3 | 任务、摘要、任务 | 检查点已保存，任务完成 |

在相同总预算下，改变 DP-F.4 会改变可用于任务推进的调用，以及所测脚本交互能否进入下一次普通请求并得到预期回答（FR-E.1）。预算耗尽仍然是显式终态，而非编排协议违约；改变 DP-E.4 则可能阻止摘要执行（FR-F.4）。预算限制（FR-E.4）在所有情形下均得到遵守。共享预算是明确设计：给摘要分配另一份无限预算会改变资源契约。这些用例不测量摘要事实准确率或任意任务成功率。

### 替换输入适配器后的审批行为

[`test_direct_file_approval_persists_in_session_and_preserves_the_mode`](../../../tests/harness/test_permission_modes_e2e.py)分别使用记录式适配器与实际普通终端 `TerminalUI` 输入路径，固定回答为先 `y` 后 `n`。二者均允许首次目标写入、在第二次写入复用该授权、拒绝另一文件，并保持 `read-only` 模式。

这支持请求、回答、策略与工作区固定时，FR-D.3 在所测 DP-A.4 替换下保持不变。不证明移除确认能力无影响、不证明所有呈现变化等价，也不表示已经测试原生 Shell 边界。

### 替换诊断查询工具

[`test_diagnostic_query_contract.py`](../../../tests/harness/test_diagnostic_query_contract.py)替换已声明查询工具的名称，并通过实际模型工具调用跟随投影引用；还覆盖待投递证据、压缩后检查点、后台结果查询、运行时工具限制与声明回滚。

新生成的诊断投影按运行时工具顺序选择第一个实际暴露且已声明的诊断查询工具。没有合格工具时，保留证据引用但不提供不可用查询指引。此契约在上述范围内保留更名后的 FR-G.5/FR-G.6，不重写旧历史，也不保证任意第三方请求预处理器再次改写最终请求上下文之后的工具可用性。

### 权限与信任设置

[提示组装](../../../tests/harness/test_prompt.py)中的 `test_permission_switch_updates_requests_and_restores_the_original_prefix` 固定会话及空消息列表，将有效模式（DP-D.1）切换为 read-only 后恢复原模式。在该场景中工具 Schema 保持相等，请求中的写入边界随模式变化（FR-F.3）。这是必要的策略传播，不证明原生隔离。

[核心插件](../../../tests/harness/test_core_plugins_e2e.py)中的 `test_project_extensions_require_trust_outside_the_project` 固定项目扩展及项目自声明的信任设置，改变外部显式信任选择（DP-D.6）。仅受信任场景执行扩展的标记文件写入（FR-H.1）。信任是加载的有意前提，而非扩展功能故障。

### 故障域边界

可选报告与权威历史有不同契约。[诊断测试](../../../tests/harness/test_diagnostics.py)中的 `test_log_write_failure_keeps_original_diagnostic_on_stderr` 和 `test_failing_ui_does_not_destroy_original_error_or_recurse` 经报告兜底保留原故障。[恢复测试](../../../tests/harness/test_recovery_lifecycle.py)中的 `test_model_diagnostics_feature_can_be_disabled_without_disabling_logs` 验证关闭查询插件后，日志和 Core 面向模型的有界证据仍保留，且不提供不可用工具的查询指引。

而 [`test_failed_save_stops_requests_and_keeps_history`](../../../tests/harness/test_harness_boundaries.py)验证权威会话持久化失败会停止后续请求、保留未保存历史。这是 DP-F.1 对 FR-E.1 的有意影响，不是可选诊断隔离失败。

其余叶级关系，包括压缩后一般模型质量变化、任意扩展请求修改和联合任务成功概率，在此没有完整功能独立性证据。其状态仍为**未验证**，而非零。

## 复现重点检查

使用锁定的开发环境与现有 pytest 入口：

```bash
uv sync --locked --dev
uv run --no-sync pytest -q tests/test_architecture_matrix.py
uv run --no-sync pytest -q tests/harness/test_compaction.py::test_auto_compaction_and_task_share_request_budget tests/harness/test_permission_modes_e2e.py::test_direct_file_approval_persists_in_session_and_preserves_the_mode tests/harness/test_diagnostic_query_contract.py
uv run --no-sync pytest -q tests/harness/test_diagnostics.py tests/harness/test_recovery_lifecycle.py tests/harness/test_harness_boundaries.py
```

根据所改行为从验收映射补选相关套件。原生保证遵循[执行验收](execution-acceptance.md)。夹具通过、跳过或导入图都不能作为信息公理的实测分数。
