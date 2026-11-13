# Virgo

**A coding agent that works where your code lives.**

[English](README.md) · [简体中文](README.zh-CN.md)

Virgo is a lightweight, extensible coding agent for your terminal. Ask it to explore a codebase, make a change, investigate a failure, or review a diff. It can read and edit files, run commands, and work through a task while you stay in the conversation.

## Why Virgo?

- **Keep working in your terminal.** A full-screen conversation, streaming tool activity, resumable sessions, and a plain terminal mode fit different workflows.
- **Choose your model.** Connect an account, bring an API key, or use a compatible local endpoint. Change models and reasoning effort during a session.
- **Give larger tasks room to run.** Delegate focused work to subagents, coordinate a team, and track background jobs without losing the main conversation.
- **Make it your own.** Add project instructions, skills, prompt templates, Python extensions, workflows, and MCP tools.
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
uv run virgo -p --mode json "Summarize the README"
```

The default `auto-review` permission mode reviews operations that cross workspace or network boundaries. Use `plan` for read-only investigation. See the [permission reference](docs/en/reference/configuration.md#permissions) for the exact behavior and limits.

## Explore

| I want to… | Start here |
| --- | --- |
| Install Virgo and connect a model | [Getting started](docs/en/guides/getting-started.md) |
| Learn commands, shortcuts, and sessions | [Using Virgo](docs/en/guides/interaction.md) |
| Configure models and permissions | [Configuration](docs/en/reference/configuration.md) |
| Add skills, tools, or workflows | [Extending Virgo](docs/en/development/extensions.md) |
| Understand or contribute to the code | [Architecture](docs/en/development/architecture.md) · [Development](docs/en/development/testing-and-ci.md) |

[All documentation](docs/en/README.md) · [中文文档](docs/zh-CN/README.md) · [Examples](examples/README.md) · [Report an issue](https://github.com/0xAstraZhang/Virgo/issues)

## License

[MIT](LICENSE). Third-party acknowledgements are in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
