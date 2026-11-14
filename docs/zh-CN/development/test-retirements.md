# 测试退役记录

[开发与 CI](testing-and-ci.md) · [CI 判定规则](testing-and-ci.md#经审查的重复测试退役) · [抽象边界](abstraction-boundaries.md) · [English](../../en/development/test-retirements.md)

## Core／插件的产品退役

审查基线：`47e1757bda5c5cfafb3a5a508a1a6ec4d629bfaf`。本次迁移退役 **46 个参数展开后的场景**，对应六个退出维护的官方产品。它们与重复测试清理分开记录：旧行为按设计不可再用，不将保留的编程测试宣称为调度器或团队平台的等价覆盖。

精确基线、原始模块、完整测试身份和参数 ID 维护于 [`core_plugin_retirements.json`](../../../ci/pipeline/core_plugin_retirements.json)。产品边界和历史数据位置见 [Core／插件边界](core-plugin-boundaries.md)与[迁移](../guides/migrating-to-core-plugins.md)。

| 退役产品 | 场景数 | 被退役的行为范围 |
| --- | ---: | --- |
| Schedule | 7 | Cron 解析、调度通知、时间／文件修复、归属，以及两个 TUI 多会话调度参数 |
| Workflow | 3 | Python 编排、日志／续跑、工具步骤及已移除引擎的非法运行 ID 拒绝 |
| Team | 8 | 长期工作线程、邮箱、计划审批、worktree、任务释放与队友专用异步／界面生命周期 |
| Tasks | 3 | 持久依赖图、任务工具和任务文件路径处理 |
| Memory | 11 | 保存／索引／整理／更新，以及八个整理／提取的模型推理强度参数 |
| Goal | 14 | 评估输入／判定、续跑循环、取消／次数限制，以及四个辅助模型推理强度参数 |

只退役六个产品测试模块及清单中明确列出的共享场景／参数。辅助请求中的权限和压缩参数保留；后台任务、子 Agent、权限、恢复、会话归属、原生协议与 TUI 交互继续执行。命令提示场景改用独立扩展命令，适应重名拒绝的契约，仍检查补全、模板、剪贴板和历史。

生产代码改动前，`tests/harness/test_core_plugins_e2e.py` 先加入八个完整会话验收场景。新契约跨越模型／工具边界，包括文件编辑和真实子进程，不通过照抄实现方法来证明正确。

CI 仅在以下条件全部满足时接受这批产品退役：

- 基线精确等于审查 SHA，原始模块／身份匹配，被删除原场景通过。
- 五个精确保留门槛在 base/head 均通过：持久化失败、权限顺序与安全失败、后台通知、子 Agent 隔离、执行期权限复查。
- 八个精确的新 Core／插件验收场景在 head 全部通过，包括旧文件／历史保留和零插件编程闭环。
- 退役身份不再出现在 head。仍存在但失败／跳过的场景接受普通回归检查；无关删除不享受例外。

每个场景仍出现在 `removed`；`retired_products` 在可读和 JSON 结果中单独记录产品决策及门槛。不按目录、前缀或数量批量豁免。基线变化需重新显式审查。比较 CLI 的验收场景在实现前加入，覆盖错误基线／模块、原场景失败、门槛缺失／失败／跳过及无关删除。

## 先前的重复测试清理

审查基线：`0ffc23707de6e87a579a46a8f027679c253b78dc`。删除 9 个未参数化用例，净减少 105 行产品测试；保留测试不改写、不重命名。删除依据是现有更完整场景已经覆盖同一行为，而非用例大小或所在测试层级。

## 删除与保留覆盖

表中名称是 CI 使用的完整函数 identity；HTTP 项还包含参数 ID。完整模块对应关系保存在 [`ci/pipeline/reports.py::APPROVED_RETIREMENTS`](../../../ci/pipeline/reports.py)。

| 删除项 | 保留场景 | 删除理由 |
| --- | --- | --- |
| `test_tool_calls_and_results_are_paired_in_order` | `test_parallel_results_are_emitted_as_completed_but_history_keeps_call_order`；`test_tool_completion_order_survives_reload_without_changing_model_history` | 保留场景强制工具乱序完成，并核对实时事件、消息历史和磁盘恢复顺序。 |
| `test_after_tool_call_overrides_result` | `test_post_tool_hook_feedback_keeps_image_results`；`test_post_tool_hook_feedback_keeps_terminate`；`test_hooks_cannot_erase_or_override_execution_and_adapter_facts` | 保留场景验证实际结果替换，并保护图片、终止标记及真实执行/传输事实。 |
| `test_result_hook_failure_preserves_the_original_tool_failure` | `test_projection_sees_final_hooks_and_recorded_evidence_once_in_provider_content` | 同样制造工具及结果钩子连续失败，并进一步核对原错误证据和 Provider 请求中的最终内容。 |
| `test_result_hook_recovery_keeps_original_diagnostic_without_marking_success_failed` | `test_tool_failure_and_recovered_result_hook_keep_original_evidence` | 同样覆盖错误被钩子恢复为成功，进一步验证日志、Runtime 和工具关联。 |
| `test_retry_on_retryable_errors` | `test_recovered_retry_exposes_its_log_reference_in_text_and_json`；`test_retry_failure_survives_success_and_reload_without_prompt_contents` | 保留场景核对重试事件、最终回答、UI/JSON 引用和恢复后的证据；旧用例额外执行真实退避等待。 |
| `test_system_prompt_includes_context_files` | `test_user_system_and_project_context_survive_for_their_roles`；`test_default_request_keeps_tool_definitions_without_duplicate_or_empty_sections` | 覆盖用户/项目指令、各角色和实际请求前缀，不止单个 `AGENTS.md` 子串。 |
| `test_model_errors_are_reported` | `test_final_model_error_is_written_once_and_exposed_with_its_log_path`；`test_provider_error_ends_run_without_crash` | 覆盖失败结束、消息、日志去重、UI 地址及持久化恢复。 |
| `test_codex_http_failure_redacts_access_token` | `test_http_failure_has_correlation_without_response_payload[429-virgo.ai.providers.codex-openai-codex]` | 统一 HTTP 矩阵已验证 429 分类、整个消息的脱敏，并检查请求关联和正文隔离。 |
| `test_responses_http_error_is_classified` | `test_http_failure_has_correlation_without_response_payload[429-virgo.ai.providers.responses-openai-responses]` | 同一矩阵覆盖 Responses 的 429 分类，并保留请求 ID、Retry-After 与脱敏检查。 |

工具/钩子场景位于 `tests/agent/` 和 `tests/harness/`；请求/提示与恢复场景位于 `tests/harness/`；HTTP 矩阵位于 `tests/ai/providers/test_error_diagnostics.py`。

## CI 约束

CI 只在原基线 SHA、原模块和精确 identity 一致，原用例为通过，且列出的保留场景在 base/head 都通过时，才将删除归类为 reviewed retirement。删除仍出现在普通 `removed` 清单，新增 `retired` 字段给出保留覆盖。不存在按目录、前缀或数量批量放行的规则。

原用例失败、保留场景缺失/跳过/失败、无关删除、不同基线、同名但不同模块，以及旧用例仍存在却失败/跳过，都继续阻止通过。用于验证这些条件的场景先加入现有 `ci/checks/comparison.py`，在旧比较器上确认失败，再修改 CI 实现；它们通过真实临时 pytest 项目和比较 CLI 验收。

其余权限、安全、取消、畸形数据、协议、并发及 TUI 行为测试保留。产品重构使用现有测试验证，没有为本次抽取新增一套实现细节测试。
