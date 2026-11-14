# Getting started

[Documentation](../README.md) · [简体中文](../../zh-CN/guides/getting-started.md)

## Install from source

Virgo requires Python 3.11 or newer. The repository uses [uv](https://docs.astral.sh/uv/) to manage the interpreter, dependencies and lockfile.

```bash
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

The installed entry point is `virgo`; `uv run python -m virgo` also works. To make the command available outside the checkout, run `uv tool install .` from the repository root, then use `virgo -C /path/to/project`.

Use `uv run virgo --help` for CLI options and `uv run virgo --version` for the installed version. [Development setup](../development/testing-and-ci.md) includes the checks needed when changing the project.

## Connect a model

Enter `/login` inside Virgo. Choose account authentication or API configuration. Login runs through the interface and saves the connection and selected model for later sessions.

| Method | Available presets | Connection |
| --- | --- | --- |
| Account (`auth`) | OpenAI, Anthropic, xAI, Google, Kimi | Browser, authorization-code or device-code flow, depending on the provider |
| API (`api`) | DeepSeek, GLM, Kimi | A preset endpoint and an API key you provide |
| Custom API | A provider ID you choose | OpenAI-compatible or Anthropic Messages endpoint, model ID and key |

Examples:

```text
/login auth openai
/login api deepseek
/login api custom
/logout
```

Follow the login flow for the endpoint and account you intend to use. Account access does not imply that every model or API product is available. Google authentication may request a Cloud project ID; `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_PROJECT_ID` are also recognized.

Existing environment credentials are supported. For example, set `ANTHROPIC_API_KEY` in your shell before starting Virgo to use the built-in Anthropic connection. Provider-specific and custom endpoint settings are documented in [Configuration](../reference/configuration.md).

Credentials are stored separately in `~/.virgo/auth.json`, or under `VIRGO_HOME`. Password inputs do not enter the chat or input history. Saved credentials are tied to the selected protocol and endpoint; log in again after changing those connection details. `/logout` removes saved credentials, while credentials still present in your environment remain available.

## Start a conversation

```bash
uv run virgo -C /path/to/project
```

Virgo opens its full-screen interface when stdin and stdout are interactive and the terminal is supported. Otherwise it uses the plain interface. You can select one explicitly:

```bash
uv run virgo --ui plain
uv run virgo --ui fullscreen
```

`--ui fullscreen` reports an error when the terminal cannot support it. Inside the conversation, `/help` lists commands and `/language 中文` or `/language English` selects the interface language. You can also select the startup language with `uv run virgo --language zh-CN`.

Try a small request first: “Find the entry point and explain how commands are registered.” You can add instructions while Virgo is running; [Using Virgo](interaction.md) explains steering, session switching and cancellation.

## Run one request or integrate with scripts

```bash
uv run virgo -p "Summarize README.md"
uv run virgo -p --mode json "List the public entry points"
git diff | uv run virgo -p "Review this diff for correctness issues"
```

Text print mode prints the final answer. JSON mode emits JSONL events; slash commands with explicit results emit `command_result` records. `-p` requires a prompt from arguments or stdin. It cannot answer interactive questions, so tools report unanswered requests instead of choosing on your behalf.

Use `--no-session` to avoid saving conversation history. This does not disable diagnostic logs or outputs created by tools. Use `--no-extensions` to skip user and project Python extensions; built-in features remain enabled unless disabled in settings.

## Try the offline demo

```bash
uv run virgo --model faux/demo
uv run virgo -p --model faux/demo "find python files"
```

The `faux` provider provides deterministic demonstrations and supports local checks without a model account. It is not a general-purpose coding model.

Next: [commands and sessions](interaction.md), [configuration](../reference/configuration.md), or [examples](../../../examples/README.md).
