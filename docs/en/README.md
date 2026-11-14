# Virgo documentation

[Project home](../../README.md) · [简体中文](../zh-CN/README.md)

Start with a task you want to complete. The guides explain everyday use; the references and development pages provide detail when you need it.

## Use Virgo

| Guide | What it covers |
| --- | --- |
| [Getting started](guides/getting-started.md) | Installation, login, your first session, print mode |
| [Using Virgo](guides/interaction.md) | Commands, keyboard shortcuts, language, sessions, steering |
| [Web search and reading](guides/web.md) | Backends, routing, permissions, fetch limits |
| [Command shells](guides/shells.md) | Shell selection, execution behavior, output and cancellation |
| [Troubleshooting](guides/troubleshooting.md) | Errors, logs, diagnostic evidence, recovery boundaries |

[Upgrading an existing installation?](guides/migrating-to-core-plugins.md) Review profile changes, project trust and preserved historical data.

## Look up a detail

- [Configuration](reference/configuration.md): precedence, settings, environment variables, permissions and stored files.
- [Tools and plugins](reference/tools.md): files, web, questions, checklists, subagents, background jobs and MCP.
- [Example files](../../examples/README.md): copy individual files into your own project.

## Extend or contribute

- [Core and plugin boundaries](development/core-plugin-boundaries.md): ownership, capability inventory and acceptance.
- [Architecture](development/architecture.md): module responsibilities, execution flow and stable boundaries.
- [Extensions](development/extensions.md): Python tools and commands, events, lifecycle, runtime services, skills and templates.
- [Localization](development/localization.md): language packs, session ownership, fallback and adding a language.
- [Development and CI](development/testing-and-ci.md): local commands, end-to-end checks, result comparison and runner maintenance.

## Keep documentation useful

Maintain matching files under `docs/en/` and `docs/zh-CN/`. Explain the current behavior and link to code for implementation details. Update both editions in the same change when a command, setting, interface or path changes. Keep release-specific investigations in issues or PRs; reusable conclusions belong in the relevant guide. The root READMEs introduce the product and link here.
