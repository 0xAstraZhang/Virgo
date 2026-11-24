# Virgo

**A coding agent that works where your code lives.**

[English](README.md) · [简体中文](README.zh-CN.md) · [Documentation](docs/en/README.md)

Virgo is a lightweight, extensible coding agent for your terminal. Ask it to explore a codebase, make a change, investigate a failure, or review a diff. It can read and edit files, run commands, and work through a task while you stay in the conversation.

> Virgo's sandbox is a **guardrail** against occasional model mistakes. It keeps writes inside the current workspace, protects private credentials and Virgo project settings, and asks before out-of-scope or high-risk operations. It is **not** a security boundary against malicious code, deliberate evasion, or prompt-injection attack chains. Run Virgo in a container or VM when working with untrusted repositories or content.

On Tier 1 platforms, Virgo starts in **Workspace-Edit** by default, constraining Shell commands with a native sandbox while asking for confirmation before out-of-scope or high-risk actions. Select **No-Limit** (`--permission no-limit` or `/permissions no-limit`) to run directly with your host account rights. See [Permissions & approvals](docs/en/guides/permissions.md) and [Execution boundaries](docs/en/guides/sandbox.md).

## Platform support

| Platform | Tier | Default mode & notes |
| --- | --- | --- |
| Linux (`bubblewrap`) | Tier 1 | **Workspace-Edit**; blocking CI and performance gates |
| macOS (Seatbelt) | Tier 1 | **Workspace-Edit**; blocking CI and performance gates |
| Native Windows 11 (Standard AppContainer) | Tier 1 | **Workspace-Edit** on builds with updated `null.sys` (unelevated, no Administrator rights); older Windows builds default to **No-Limit** with a one-time notice. Honest loopback and `$HOME` limits apply ([details](docs/en/guides/windows-sandbox.md)) |
| Windows + WSL2 (Ubuntu / Linux) | Tier 1 | **Workspace-Edit** via the Linux `bubblewrap` backend; recommended for older Windows builds, Linux toolchains, or sandboxed localhost/loopback workflows ([WSL2 guide](docs/en/guides/wsl2.md)) |

## Why Virgo?

- **Keep working in your terminal.** A full-screen conversation, streaming tool activity, resumable sessions, and a plain terminal mode fit different workflows.
- **Choose your model.** Connect an account, bring an API key, or use a compatible local endpoint. Change models and reasoning effort during a session.
- **Give larger tasks room to run.** Delegate focused work to subagents and track background jobs without losing the main conversation.
- **Make it your own.** Add project instructions, skills, prompt templates, and Python extensions, alongside built-in MCP and Todos extensions.
- **Choose your interface language.** Switch interface messages, command help, and built-in tool descriptions with `/language 中文` or `/language English`. Virgo replies in the language of your message unless you ask for another.

Virgo keeps its model adapters, agent loop, execution layer, and session harness separate so you can extend the parts you need.

## Get started

You need **Python 3.11+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

In Virgo, enter `/login` to connect a supported account or configure an API. API key login reads the server's model list. It keeps your current model for that provider if listed, then prefers the provider preset, then the first model returned. Use `/model` to switch. Only **API → Custom** asks for a model ID when the server reports that it does not support `/models` (HTTP 404, 405, or 501).

Once connected, try asking Virgo to explain the project.

```text
Explain how this project starts and where I should add a new command.
```

To try the interface before connecting a model, run the offline scripted demo (no credentials required):

```bash
uv run virgo --model faux/demo
```

## Fit it into your workflow

```bash
# Work in another project
uv run virgo -C /path/to/project

# Run a single request
uv run virgo -p "Review the current diff for correctness issues"

# Continue your most recent session (or use -r <path> for a specific session)
uv run virgo --continue

# Integrate with other programs
uv run virgo -p --format json "Summarize the README"
```

- **Full-screen UI**: `Enter` sends a message, `Ctrl+J` inserts a newline, `/` browses commands, `Shift+Tab` cycles permission modes, `Ctrl+T` expands reasoning, `Ctrl+O` expands tool details, and `Esc` interrupts a running reply. Click a tool call row (or focus it and press `Enter`) to inspect its result.
- Virgo sets **reasoning effort** to the default advertised for the selected model. If the directory omits it, Virgo uses `high`, or the first supported level if `high` is unavailable. You can override this with `/effort`, `--effort`, or the `effort` setting.
- **Sessions & recovery**: `--continue` opens the most recently updated session in the workspace. If its final record is incomplete and lacks a trailing newline, Virgo trims the incomplete tail and keeps the valid prefix; corrupt headers or complete records halt startup with the file and line number.
- **Status & permissions**: `/status` shows session ID, model, working directory, token usage, and active permissions. `/permissions` summarizes the active mode and boundaries (`/permissions show` or `-p /permissions` prints full JSON); switch modes with `/permissions <mode>`, `--permission <read-only|workspace-edit|no-limit>`, or `Shift+Tab`.
- **Diagnostics**: `/diagnostics` lists the latest ten errors stored in `~/.virgo/logs/diagnostics.sqlite3` (or `$VIRGO_HOME/logs/diagnostics.sqlite3`). Use `--before <cursor>` for older pages or `/diagnostics <error-id>` for full details; review details before sharing.

## Documentation

Browse the full [documentation hub](docs/en/README.md):

- **Guides**: [Permissions & approvals](docs/en/guides/permissions.md) · [Execution boundaries](docs/en/guides/sandbox.md) · [Native Windows sandbox](docs/en/guides/windows-sandbox.md) · [WSL2 setup](docs/en/guides/wsl2.md) · [Extensions (Todos & MCP)](docs/en/guides/extensions.md)
- **Development**: [Architecture](docs/en/development/architecture.md) · [Execution interface](docs/en/development/execution-interface.md) · [Execution acceptance](docs/en/development/execution-acceptance.md)
- **Decisions**: [Architectural decisions](docs/en/decisions/README.md)
- **Examples & community**: [Project examples](examples/README.md) · [Report an issue](https://github.com/0xAstraZhang/Virgo/issues)

## License

[MIT](LICENSE).
