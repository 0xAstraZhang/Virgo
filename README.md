# Virgo

**A coding agent that works where your code lives.**

[English](README.md) · [简体中文](README.zh-CN.md)

Virgo is a lightweight, extensible coding agent for your terminal. Ask it to explore a codebase, make a change, investigate a failure, or review a diff. It can read and edit files, run commands, and work through a task while you stay in the conversation.

> Virgo's sandbox is a **guardrail** against occasional model mistakes. It keeps their effects inside the current workspace, protects Virgo's private data and project control files, and asks before out-of-scope or high-risk operations. It is **not** a security boundary against malicious code, deliberate evasion, or prompt-injection attack chains. Run Virgo in a container or VM when working with untrusted repositories or content.

On Linux, macOS, WSL2, and native Windows, Virgo starts in **Workspace-Edit**, using native Shell sandboxing against the live project with operation-specific approvals. Select **No-Limit** (`--permission no-limit` or `/permissions no-limit`) to use the current host account. Native Windows uses Standard AppContainer with no Administrator privileges required; WSL2 remains a Tier 1 option if you prefer Linux tooling or need localhost/loopback access inside sandboxed commands. See [execution boundaries](docs/en/guides/sandbox.md), [native Windows sandbox](docs/en/guides/windows-sandbox.md), and [WSL2 setup](docs/en/guides/wsl2.md).

## Platform support

| Platform | Tier | Notes |
| --- | --- | --- |
| Linux (`bubblewrap` + `libseccomp2`) | Tier 1 | Full support, default **Workspace-Edit**, blocking CI & performance gates |
| macOS (Seatbelt) | Tier 1 | Full support, default **Workspace-Edit**, blocking CI & performance gates |
| Native Windows (Standard AppContainer) | Tier 1 | Full support, default **Workspace-Edit**, unelevated (no Administrator rights), session-level single SID, blocking CI & performance gates; loopback (`127.0.0.1`/`[::1]`) and `$HOME` workspaces have honest platform limits ([details](docs/en/guides/windows-sandbox.md)) |
| Windows + WSL2 (Ubuntu / Linux) | Tier 1 | Uses the Linux sandbox backend — ideal for Linux toolchains or sandboxed localhost/loopback workflows ([WSL2 guide](docs/en/guides/wsl2.md)) |

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

Use `/permissions show` to inspect the active task and next-task mode. Select one of the three modes with `/permissions` or `--permission <read-only|workspace-edit|no-limit>`. See [permission modes and grants](docs/en/development/permission-profiles.md).

## Inspect errors

Run `/diagnostics` to list the latest ten errors and show the database path.
Use `/diagnostics --before <cursor>` for earlier pages or
`/diagnostics <error-id>` to view details for an exact ID or a unique prefix.

Errors remain available across restarts in `~/.virgo/logs/diagnostics.sqlite3`
(or `$VIRGO_HOME/logs/diagnostics.sqlite3`). The command incrementally imports
older JSONL logs and leaves the source files in place. Review diagnostic details
before sharing them.

## Explore

[Execution boundaries](docs/en/guides/sandbox.md) · [WSL2 guide](docs/en/guides/wsl2.md) · [Install optional extensions](extensions/README.md) · [Examples](examples/README.md) · [Report an issue](https://github.com/0xAstraZhang/Virgo/issues)

## License

[MIT](LICENSE). Third-party acknowledgements are in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
