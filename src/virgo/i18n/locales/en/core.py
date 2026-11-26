"""Language selection and shared UI messages."""

MESSAGES = {
    "core.agent.aborted": "aborted by user",
    "core.agent.hook_failed": "Tool call hook failed: {error}",
    "core.agent.invalid_args": "Invalid arguments:\n{errors}",
    "core.agent.invalid_json": "Tool arguments were not a valid JSON object. Re-issue the call.",
    "core.agent.length_continue": "The output token limit cut your reply short. Pick up where you left off. "
    "Make the required tool call or finish the answer concisely without repeating earlier text or reasoning.",
    "core.agent.length_continue_empty": "You reached the output token limit before giving a visible answer. "
    "Go directly to the required tool call or give a concise answer without repeating your reasoning.",
    "core.agent.operation_aborted": "Operation aborted.",
    "core.agent.permission_failed": "Execution permission check failed: {error}",
    "core.agent.result_hook_failed": "Tool result hook failed: {error}",
    "core.agent.search_paused": "[Search paused after three continuations. Send another message to resume.]",
    "core.agent.truncated_arguments": "The response hit the output token limit, so these tool arguments may "
    "be truncated. Re-issue the call (split large content into smaller "
    "steps).",
    "core.agent.turn_limit": "[Stopped: reached the {turns}-turn limit.]",
    "core.agent.unknown_tool": "Unknown tool: {name}. Available: {tools}",
    "language.description": "show or change the interface language",
    "language.usage": "Usage: /language [{languages}]",
    "language.current": "Language: {language}. {usage}",
    "language.changed": "Language changed to {language}. Saved as your default; project/runtime settings take precedence on restart.",
    "language.invalid": "Unsupported language: {value}. {usage}",
    "language.busy": "Cannot switch languages while the agent is running. Try again when it finishes.",
    "language.save_failed": "Language was not changed because the preference could not be saved: {error}",
    "common.unavailable": "unavailable",
    "sandbox.host_execution": "Host · OS permissions",
}
