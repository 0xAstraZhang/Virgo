# Virgo

**A coding agent that works where your code lives.**

[English](README.md) · [简体中文](README.zh-CN.md)

Virgo is a lightweight, extensible coding agent for your terminal. Ask it to explore a codebase, make a change, investigate a failure, or review a diff. It can read and edit files, run commands, and work through a task while you stay in the conversation.

Virgo starts in **Workspace-Edit**, using native isolation against the live project with operation-specific approvals. Select **No-Limit** to use the current host account. See [execution boundaries](docs/en/guides/sandbox.md).

## Why Virgo?

- **Keep working in your terminal.** A full-screen conversation, streaming tool activity, resumable sessions, and a plain terminal mode fit different workflows.
- **Choose your model.** Connect an account, bring an API key, or use a compatible local endpoint. Change models and reasoning effort during a session.
- **Give larger tasks room to run.** Delegate focused work to subagents and track background jobs without losing the main conversation.
- **Make it your own.** Add project instructions, skills, prompt templates and Python extensions, including optional MCP and Todos extensions.
- **Use Chinese or English.** Switch interface messages, command help, and built-in tool descriptions with `/language 中文` or `/language English`.

Virgo is inspired by [Pi](https://github.com/earendil-works/pi). Its model adapters, agent loop, and session harness are separate, so you can extend the parts you need.

## Get started

You need **Python 3.11+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

In Virgo, enter `/login` to connect a supported account or configure an API. Then ask for something concrete:

```text
Explain how this project starts and where I should add a new command.
```

Want to try the interface before connecting a model? The offline demo needs no credentials:

```bash
uv run virgo --model faux/demo
```

The demo uses scripted responses; connect a real model for coding work.

## Fit it into your workflow

```bash
# Work in another project
uv run virgo -C /path/to/project

# Run a single request
uv run virgo -p "Review the current diff for correctness issues"

# Continue your most recent session
uv run virgo --continue

# Integrate with other programs
uv run virgo -p --format json "Summarize the README"
```

Use `/permissions show` to inspect the active task and next-task mode. Select one of the three modes with `/permissions`. See [permission modes and grants](docs/en/development/permission-profiles.md).

## Inspect errors

Run `/diagnostics` to list the latest ten errors and show the database path.
Use `/diagnostics --before <cursor>` for earlier pages or
`/diagnostics <error-id>` to view details for an exact ID or a unique prefix.

Errors remain available across restarts in `~/.virgo/logs/diagnostics.sqlite3`
(or `$VIRGO_HOME/logs/diagnostics.sqlite3`). The command incrementally imports
older JSONL logs and leaves the source files in place. Review diagnostic details
before sharing them.

## Explore

[Install optional extensions](extensions/README.md) · [Examples](examples/README.md) · [Report an issue](https://github.com/0xAstraZhang/Virgo/issues)

## License

[MIT](LICENSE). Third-party acknowledgements are in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
