# Virgo examples

[Project home](../README.md) · [简体中文](README.zh-CN.md)

These files show how a project can extend Virgo. Copy only the parts you need into your own project, adapting paths and model IDs. Merge settings with any existing configuration.

## File map

| Example | Destination in your project | Purpose |
| --- | --- | --- |
| [Project instructions](project/AGENTS.example.md) | `AGENTS.md` at the project root | Shared instructions read with project context |
| [Settings](project/.virgo/settings.json) | `.virgo/settings.json` | Language, effort, shell and an optional local API |
| [MCP configuration](project/.virgo/mcp.json) | `.virgo/mcp.json` | Disabled placeholder for the [MCP extension](../extensions/README.md) |
| [Secret-file guard](project/.virgo/extensions/protect_env.py) | `.virgo/extensions/protect_env.py` | A tool hook and a `/stats` command |
| [Review template](project/.virgo/prompts/inspect.md) | `.virgo/prompts/inspect.md` | `/inspect` with arguments |
| [Code-review skill](project/.virgo/skills/code-review/SKILL.md) | `.virgo/skills/code-review/` | A skill with a separate checklist |

`project/.virgo/` mirrors Virgo's runtime project configuration directory. The enclosing `project/` keeps the sample discoverable; it is not part of the destination path. `AGENTS.example.md` belongs at the project root, not inside `.virgo/`.

## Try a selected example

After copying the files, start Virgo in the destination project. For example, in a POSIX shell:

```bash
mkdir -p /path/to/project/.virgo/prompts
cp examples/project/.virgo/prompts/inspect.md /path/to/project/.virgo/prompts/inspect.md
```

Copy the code-review skill as well if you want the review template's named skill available. Restart Virgo after adding extensions, templates, skills or agent types.

After reviewing the Python guard, project `providers`, or `.virgo/mcp.json`, enable them by adding the project directory to user-level `extensions.trustedProjects` or passing `--trust-project-extensions`; untrusted projects cannot enable extensions, MCP servers, hooks, or custom providers.

The settings example's `language` pins that project's language; use `zh-CN` for Chinese or omit the field to follow the user default. The optional `local` provider needs a real server, its installed model ID/context window, and project trust (`extensions.trustedProjects` or `--trust-project-extensions`). It does not change the selected model until you choose it. Obsolete `smallModel` overrides are intentionally absent because auxiliary models are selected by the runtime.

The MCP example is disabled and uses a placeholder path. MCP itself comes from the [extension](../extensions/README.md); install that and replace the placeholder with your own stdio server command and arguments before enabling it. Example instructions and prompts are authored resources; `/language` does not rewrite their content.
