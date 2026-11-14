# Using Virgo

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/interaction.md)

## Conversation and language

Type a request and press Enter. While the agent is working, another message steers the current run: it is inserted after the current tool batch and before the next model call. Read-only commands can run concurrently; commands that change state wait for the active run to finish.

```text
/language 中文
/language English
/language
```

The first two commands switch the current session and save a user default. `/language` shows the current language and available choices. Canonical settings values are `zh-CN` and `en`. Language affects Virgo's own interface, command descriptions and built-in tool metadata. Command names, parameter keys, model IDs and machine-readable values stay stable. The selected language is also offered to the model as a response preference; an explicit user request wins. Conversation history, file content and third-party output retain their original text. See [localization](../development/localization.md) for extension support.

## Commands

`/help` lists commands currently registered for your configuration, including prompt templates. Disabled plugins do not register their commands. The default `coding` preset and optional capabilities are described in [Configuration](../reference/configuration.md#profiles-and-plugins); removed commands are listed in [Migration](migrating-to-core-plugins.md). The full-screen editor shows an argument hint after a complete command name; the hint disappears when you type arguments and is never submitted as input.

| Command | Purpose |
| --- | --- |
| `/help` | List commands and prompt templates |
| `/language [中文\|English]` | Show or change the interface language |
| `/login [auth\|api] [provider]` | Connect an account or API |
| `/logout [provider\|all]` | Remove saved credentials |
| `/model [provider/id]` | Pick a model or switch directly |
| `/effort [low\|medium\|high\|xhigh\|max]` | Pick or set reasoning effort |
| `/shell [name\|path\|auto]` | Choose the command shell |
| `/search [auto\|duckduckgo\|deepseek\|off]` | Choose search behavior |
| `/fetch [auto\|http\|deepseek\|off]` | Choose webpage-reading behavior |
| `/permissions [auto-review\|accept-all\|plan]` | Inspect rules or change permission mode |
| `/compact [focus]` | Compact older model context |
| `/new`, `/sessions [number]`, `/session` | Start, browse or inspect sessions |
| `/diagnostics [error-id]` | Inspect recent errors and diagnostic records |
| `/todos`, `/jobs` | Inspect the conversation checklist or background jobs |
| `/skills`, `/skill:name [args]` | List skills or explicitly use one |
| `/mcp [reconnect]` | Inspect or reload MCP connections |
| `/exit` | Exit the interactive application |

Model and effort pickers use the saved model catalog. They do not start a network refresh. Virgo refreshes connected providers in the background at startup; if the first catalog is still loading, reopen `/model` after it completes. A failed refresh keeps the previous catalog. Enter saves the picker choice as a user default; `s` applies it only to the current session; Esc cancels. Direct `/model provider/id` and `/effort high` commands affect the current session.

## Full-screen shortcuts

| Key | Action |
| --- | --- |
| Enter | Send input; confirm the selected item in a menu |
| Shift+Enter | Insert a newline; use Ctrl+J in macOS Terminal |
| Tab | Complete slash commands and prompt templates |
| Shift+Tab | Cycle permission modes for this session |
| ↑ / ↓ | Navigate a menu; at editor boundaries, browse input history |
| Ctrl+P / Ctrl+N | Browse input history directly |
| Ctrl+O / Ctrl+T | Toggle tool details / thoughts |
| Ctrl+R | Open the session browser |
| PageUp / PageDown | Scroll the conversation or open details |
| Ctrl+Home | Go to the beginning |
| Esc | Close a menu; otherwise interrupt a run; reject an approval |
| Ctrl+C | Interrupt a run, clear input, or exit with a second press within two seconds when input is empty |
| Ctrl+D | Exit when input is empty |

In macOS Terminal, Enter and Shift+Enter can arrive as the same key sequence, so the interface advertises Ctrl+J for newlines. Other terminals must send distinguishable modified-key sequences for Shift+Enter to work.

Messages stay expanded; thoughts, tool calls and results can be folded independently. Scroll upward to inspect history without following incoming output; return to the bottom or click the latest-message control to resume following it. Text selection copies on release; password fields do not copy automatically. macOS uses its clipboard, while other terminals need OSC52 support.

Approval panels initially select denial. Use arrows and Enter, `y/n/a`, or `1/2/3` for allow once, deny or allow for this session. Long questions are scrollable. Permission details and the meaning of each mode are in [Configuration](../reference/configuration.md#permissions).

## Sessions and workspaces

`--continue` opens the latest session for the working directory. `--resume [FILE]` opens a file or presents a picker. The full-screen `/sessions` command and Ctrl+R browse all workspaces:

- Enter opens a session; `n` starts a new one in the current workspace; Esc returns.
- `g` switches between workspace grouping and a flat list. The choice is saved.
- Ctrl+X moves a saved session into the system trash. Running sessions and sessions awaiting input cannot be trashed.

Opening another conversation does not stop the original run. Each live conversation keeps its own input draft, pending questions, event stream and cancellation state. Opening a different workspace loads that workspace's settings, tools and extensions. Exiting Virgo stops the application's active work.

Session history is stored as JSONL under the user data directory. Compaction changes the context sent to the model while retaining full history for replay. Newly opened empty sessions are cleaned up at exit. A save failure pauses further work and keeps unsaved in-memory history available; see [Troubleshooting](troubleshooting.md).

The plain interface keeps numbered prompts and line-oriented input. Ctrl+C interrupts a run; press it twice within two seconds while idle to exit.

Implementation: [CLI](../../../src/virgo/cli), [commands](../../../src/virgo/harness/commands.py), [session store](../../../src/virgo/harness/store.py).
