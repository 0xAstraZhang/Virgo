# Virgo examples

[Project home](../README.md) · [简体中文](README.zh-CN.md)

These files show how a project can extend Virgo. Copy only the parts you need into your own project, adapting paths and model IDs. Merge settings with any existing configuration.

## File map

| Example | Destination in your project | Purpose |
| --- | --- | --- |
| [Project instructions](project/AGENTS.example.md) | `AGENTS.md` at the project root | Shared instructions read with project context |
| [Settings](project/.virgo/settings.json) | `.virgo/settings.json` | Language, effort, shell, permissions and an optional local API |
| [MCP configuration](project/.virgo/mcp.json) | `.virgo/mcp.json` | Disabled placeholder for a stdio server |
| [Secret-file guard](project/.virgo/extensions/protect_env.py) | `.virgo/extensions/protect_env.py` | A tool hook and a `/stats` command |
| [Review template](project/.virgo/prompts/review.md) | `.virgo/prompts/review.md` | `/review` with arguments |
| [Code-review skill](project/.virgo/skills/code-review/SKILL.md) | `.virgo/skills/code-review/` | A skill with a separate checklist |
| [Reviewer agent](project/.virgo/agents/reviewer.md) | `.virgo/agents/reviewer.md` | A read-only subagent type |
| [Review workflow](project/.virgo/workflows/review-changes.py) | `.virgo/workflows/review-changes.py` | Parallel review with a verification stage |

`project/.virgo/` mirrors Virgo's runtime project configuration directory. The enclosing `project/` keeps the sample discoverable; it is not part of the destination path. `AGENTS.example.md` belongs at the project root, not inside `.virgo/`.

## Try a selected example

After copying the files, start Virgo in the destination project. For example, in a POSIX shell:

```bash
mkdir -p /path/to/project/.virgo/prompts
cp examples/project/.virgo/prompts/review.md /path/to/project/.virgo/prompts/review.md
```

Copy the code-review skill as well if you want the review template's named skill available. Restart Virgo after adding extensions, templates, skills, agent types or workflow metadata.

The settings example's `language` pins that project's language; use `zh-CN` for Chinese or omit the field to follow the user default. The optional `local` provider needs a real server and its installed model ID/context window. It does not change the selected model until you choose it. Obsolete `smallModel` overrides are intentionally absent because auxiliary models are selected by the runtime.

The MCP example is disabled and uses a placeholder path. Replace it with your own stdio server command and arguments before enabling it. Example instructions, prompts and workflow text are authored resources; `/language` does not rewrite their content.

See [Extensions](../docs/en/development/extensions.md) for the API, [Configuration](../docs/en/reference/configuration.md) for precedence and [Tools and automation](../docs/en/reference/tools.md) for runtime behavior.
