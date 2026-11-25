"""English features messages."""

MESSAGES = {
    "features.agent.aborted": "Sub-agent was aborted.",
    "features.agent.background": "Started background agent {id} ({agent_id}: {description}). You will be notified when it "
    "finishes; use job_output to read its result.",
    "features.agent.background_disabled": "Background execution is disabled for this session.",
    "features.agent.busy": "Sub-agent {id} is already running.",
    "features.agent.failed": "Sub-agent failed: {error}",
    "features.agent.list_empty": "No sub-agents in this session.",
    "features.agent.total_turn_limit": "This sub-agent has used all {turns} assistant turns allowed by "
    "subagents.maxTotalTurns in this session. Inspect its completed work before continuing.",
    "features.agent.turn_limit": "Sub-agent stopped at its {turns}-turn limit without finishing. "
    "Inspect completed work before delegating a smaller task or increasing subagents.maxTurns.",
    "features.agent.incomplete": "Sub-agent stopped without a completed answer: {reason}. "
    "Check the failure and completed work before deciding how to continue.",
    "features.agent.no_answer": "The sub-agent produced no answer.",
    "features.agent.progress": "\n"
    "The child returned {count} tool result(s) before stopping; earlier actions may "
    "already have taken effect. Recent results:\n",
    "features.agent.unknown_id": "Unknown sub-agent {id!r} in this session.",
    "features.ask_user.answered": "The user answered: {answer}",
    "features.ask_user.cancelled": "The question was cancelled without an answer.",
    "features.ask_user.chose": "The user chose {letter}: {answer}",
    "features.ask_user.custom": "{letter}. Type my own answer",
    "features.ask_user.declined": "The user dismissed the question without answering.",
    "features.ask_user.empty": "question is empty",
    "features.ask_user.empty_option": "every option needs a non-empty label",
    "features.ask_user.minute": "{count} minute",
    "features.ask_user.minutes": "{count} minutes",
    "features.ask_user.no_user": "The question was not answered: this session is not interactive.",
    "features.ask_user.recommended": " (recommended)",
    "features.ask_user.seconds": "{seconds:g} seconds",
    "features.ask_user.timed_out": "The user did not answer within {duration}; the question was withdrawn.",
    "features.commands.fetch": "choose page reader (/fetch [auto|http|deepseek|off])",
    "features.commands.jobs": "list background jobs",
    "features.commands.permissions": "show or change permission presets, approval policy and remembered decisions",
    "features.commands.search": "choose search backend (/search [auto|duckduckgo|deepseek|off])",
    "features.commands.skills": "list available skills (/skill:name to use one)",
    "features.commands.subagents": "inspect sub-agent traces in this session",
    "features.diagnostics.failed": "Diagnostic lookup failed; the original result is unchanged.",
    "features.diagnostics.runtime": "Diagnostic lookup requires a trusted calling runtime.",
    "features.diagnostics.unavailable": "This runtime has no diagnostic evidence collection.",
    "features.fetch.backend": "fetch.backend must be http or deepseek",
    "features.fetch.key": "DeepSeek fetch requires an official API key. Use /login api deepseek or "
    "DEEPSEEK_API_KEY.",
    "features.fetch.max_tokens": "fetch.maxContentTokens must be an integer from 1 to 100000",
    "features.fetch.mode": "fetch.mode must be auto, native, builtin or off",
    "features.fetch.selection": "Fetch backend must be auto, http, deepseek or off.",
    "features.jobs.empty": "No background jobs.",
    "features.jobs.exit_code": ", exit code {code}",
    "features.jobs.kind.agent": "agent",
    "features.jobs.kind.bash": "bash",
    "features.jobs.kind.command": "command",
    "features.jobs.kind.powershell": "powershell",
    "features.jobs.last_output": "Last output",
    "features.jobs.missing": "No job {id}. Known: {known}",
    "features.jobs.more": "\n"
    "[{total} {unit} total — use job_output id={id} for the log path and bounded output]",
    "features.jobs.no_output": "(no output yet)",
    "features.jobs.no_shell": "No shell is available; use /shell to select one.",
    "features.jobs.none": "none",
    "features.jobs.notification": "Background job {id} ({kind}: {label}) {status}{code} after {elapsed}.\n"
    "{heading}:\n"
    "{output}",
    "features.jobs.result": "Result",
    "features.jobs.state.completed": "completed",
    "features.jobs.state.failed": "failed",
    "features.jobs.state.killed": "killed",
    "features.jobs.state.running": "running",
    "features.jobs.stopping": "Stopping {id}.",
    "features.jobs.unit.bytes": "bytes",
    "features.jobs.unit.chars": "chars",
    "features.permission.read_paths": "Read access: {path}",
    "features.permission.write_paths": "Write access: {path}",
    "features.permission.network_allowed": "Shell network: allowed",
    "features.permission.user_declined": "The user declined this permission request. Do not repeat it or request "
    "a broader grant for this access in the current turn.",
    "features.permission.external_declined": "The user declined this external tool call. It was not approved. "
    "Do not retry it or request a broader grant for it in the current turn.",
    "features.permission.already_denied_this_turn": "The user already denied access covered by this request in "
    "the current turn. Do not repeat the request or ask for a broader grant.",
    "features.permission.confirmation_unavailable": "Additional resources were not granted because interactive "
    "confirmation is unavailable.",
    "features.permission.cancelled": "The permission request was cancelled.",
    "features.permission.cancelled_or_expired": "The permission request expired because its context changed.",
    "features.permission.approval_disabled": "Permission requests are disabled by the approval policy.",
    "features.permission.protected_path": "Protected resources cannot be granted: {path}",
    "features.permission.high_risk_user_declined": "The user declined to execute this high-risk command. {risk} "
    "The command did not start. Do not retry unless the user asks.",
    "features.permission.high_risk_confirmation_unavailable": "Interactive confirmation is unavailable for this "
    "high-risk command. {risk} The command did not start.",
    "features.permission.high_risk_cancelled": "Confirmation for this high-risk command was cancelled. {risk} "
    "The command did not start.",
    "features.permission.high_risk_cancelled_or_expired": "Confirmation for this high-risk command expired "
    "because its context changed. {risk} The command did not start.",
    "features.permission.high_risk_approval_disabled": "The approval policy has disabled confirmation for this "
    "high-risk command. {risk} The command did not start.",
    "features.search.key": "DeepSeek search requires an official API key. Use /login api deepseek with "
    "https://api.deepseek.com or set DEEPSEEK_API_KEY (search.apiKeyEnv can select a "
    "separate search key).",
    "features.search.legacy": "Brave search was removed; the client backend is now DuckDuckGo. Use /search to "
    "choose.",
    "features.search.mode": "search.mode must be auto, native, builtin or off",
    "features.search.query": "Search query, at most {limit} characters.",
    "features.search.selection": "Search backend must be auto, duckduckgo, deepseek or off.",
    "features.skills.empty": "No skills found. Add <dir>/SKILL.md under ~/.virgo/skills or .virgo/skills.",
    "features.skills.none": "none",
    "features.skills.unknown": "Unknown skill {name!r}. Available: {available}",
    "features.web.client_disabled": "Web {operation} is provided by the selected native tool; client execution "
    "is disabled.",
    "features.web.combined": "combined capability disabled or explicitly assigned to a client backend",
    "features.web.context": "Web {operation} requires the calling agent's model context.",
    "features.web.credentials": "{backend} credentials unavailable; configure /login api {backend}",
    "features.web.disabled": "Web {operation} is disabled.",
    "features.web.native_client": "Native web {operation} is unavailable for this client call.",
    "features.web.native_unavailable": "Native web {capability} is unavailable for {model}: {reason}; use "
    "{capability}.mode=auto or builtin.",
    "features.web.no_adapter": "no eligible capability adapter or credentials",
    "features.web.permissions": "native scope cannot satisfy permissions or tool hooks",
    "features.web.rejected": "provider rejected this capability",
    "features.web.requires_capabilities": "combined native tool requires disabled or unassigned capabilities",
    "features.web.max_calls": "plugins.web.maxCalls must be null or a positive integer.",
    "features.web.call_limit": "Web tools reached plugins.web.maxCalls for this user request.",
    "features.web.operation_limit": "Web {operation} reached {operation}.maxUses for this user request.",
    "features.web.budget_requires_client": "An explicit shared web call budget requires client tools.",
    "features.search.max_uses": "search.maxUses must be null or a positive integer.",
    "features.fetch.max_uses": "fetch.maxUses must be null or a positive integer.",
}
