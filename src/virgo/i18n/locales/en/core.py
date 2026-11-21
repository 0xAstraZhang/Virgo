"""Language selection and shared UI messages."""

MESSAGES = {
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
