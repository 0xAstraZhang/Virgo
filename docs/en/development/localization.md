# Localization

[Documentation](../README.md) · [简体中文](../../zh-CN/development/localization.md)

## User-facing contract

Virgo ships `en` (English) and `zh-CN` (中文). Select a language with:

```text
/language 中文
/language English
```

`/language` alone lists the current language and registered choices. Names and aliases resolve to a canonical code; matching ignores case and accepts underscores instead of hyphens. Built-in aliases include `zh`, `Chinese`, `简体中文`, `en-US` and `en-GB`.

Startup precedence is the built-in `en` default, user `settings.language`, project `settings.language`, then the runtime/`--language` override. There is no `VIRGO_LANGUAGE` environment variable. For example:

```bash
uv run virgo --language zh-CN
uv run virgo --language English --help
```

A slash-command change saves only `language` in user settings and then applies it to the current session. A failed save leaves the active language unchanged. If a custom UI fails to refresh after a successful change, Virgo reports `ui.language_changed` and keeps the committed preference; it does not report a save rollback. The command waits for the current main run; direct API calls reject a busy session. Project and runtime overrides can take precedence again on restart. Existing live sessions retain their own selection; newly created sessions use their effective settings, including any explicit override inherited by their controller.

The selection controls interface messages, built-in command descriptions, tool labels and natural-language schema descriptions. It also supplies a model response-language preference; an explicit user request takes precedence. It does not translate saved conversation content, user files, identifiers, enum values, provider/MCP responses or arbitrary extension prose.

## Package and ownership

[`virgo.i18n`](../../../src/virgo/i18n/__init__.py) contains the public API. Built-in catalogs live in [`locales/en`](../../../src/virgo/i18n/locales/en) and [`locales/zh_CN`](../../../src/virgo/i18n/locales/zh_CN), each split into `core`, `harness`, `ui`, `features` and `tools` modules exporting `MESSAGES` dictionaries.

| API | Responsibility |
| --- | --- |
| `LanguagePack(code, name, messages, aliases=())` | Canonical code, native name, keyed messages and accepted aliases |
| `register_language(pack)` | Validate and register an additional language before sessions start |
| `available_languages()` | Return registered packs for command/UI choices |
| `normalize_language(value)` | Resolve a name/code/alias or reject an unsupported value |
| `Translator(language="en")` | Own one mutable language selection |
| `translator(key, **values)` / `translator.text(...)` | Resolve and format a message |
| `translator.has(key)` | Check the selected or fallback catalog |
| `translator.set_language(value)` | Change this translator's selection |
| `session.t`, `api.t`, `ctx.t` | Translate using the owning session/tool context |
| `session.set_language(value, persist=True)` | Apply a validated session change, optionally saving the preference |

The registry contains immutable pack snapshots. Current language is never a process-global switch. UI components, background callbacks and tool execution use their owning translator, so one conversation cannot change another's language by writing global state. Standalone contexts without an injected translator use English.

## Message keys and formatting

Use stable semantic keys such as `language.changed` or `tools.read.description`. Store whole sentences with named placeholders:

```python
# English catalog
MESSAGES = {"example.saved": "Saved {count} files to {path}."}

# Chinese catalog
MESSAGES = {"example.saved": "已将 {count} 个文件保存到 {path}。"}

# Call site, after the key has been integrated into the catalogs
text = session.t("example.saved", count=3, path=destination)
```

Do not construct keys from display text or split sentences into translated fragments. Preserve placeholder names across languages. Registration validates formatting and compares placeholder sets with the English entry when present; placeholders must be named identifiers, and conversions are limited to `!s`, `!r` and `!a`. Escape literal braces with `{{` and `}}`. Supply all required formatting values at the call site.

Resolution checks the selected pack, then English. If the selected message cannot format the supplied values, the formatter retries the English fallback; invalid arguments still raise if that fallback also cannot format. A key absent from both is returned as the key itself, making missing integration visible. Partial additional packs can therefore fall back to English, but the two shipped editions should stay complete and synchronized. Use distinct keys when grammar requires distinct forms; the current formatter does not implement a plural-rules engine.

## Commands and tools

Command registration accepts `description_key` and `argument_hint_key`. Their translated values are resolved when commands are retrieved, so changing languages does not require re-registering every command. The literal name, handler and concurrency behavior stay unchanged. Language choices are generated from the registry.

`AgentTool.i18n_key` opts a tool into metadata localization. For a prefix `tools.read`, relevant keys include:

| Metadata | Catalog key |
| --- | --- |
| Tool label | `tools.read.label` |
| Tool description | `tools.read.description` |
| Parameter description | `tools.read.parameters.properties.path.description` |
| Nested schema text | A path through actual schema containers, ending in `description` or `title` |

`i18n_values` supplies a mapping, or a callable receiving the translator, for dynamic label/description placeholders such as actual shell details. `localize_tool` copies metadata and schemas; it does not mutate shared tool constants. For built-in keys, an opted-in field translates only when its original text matches the canonical English entry, preserving explicitly overridden extension prose. A custom tool can use keys supplied by an additional language pack even without a canonical English entry; missing translations fall back to the tool's original text. Only schema annotations are translated. Property names, execution functions and values under `default`, `const`, `enum`, `examples` and `required` remain intact, even when a payload contains fields named `description` or `title`.

When extension-supplied metadata or formatting fails during model-request assembly, the runtime records an `i18n.tool` diagnostic and uses the original `ToolSpec`. The localization failure does not remove an otherwise usable tool.

Tool results translate at the point where Virgo creates its own explanatory text through `ctx.t` or `api.t`. File contents, command output and remote payloads are data and must not be passed through a translation catalog.

## Add a language

A host application can register a new pack before creating `AgentSession` or invoking the CLI:

```python
from virgo.i18n import LanguagePack, register_language
from virgo.cli.main import main

register_language(
    LanguagePack(
        code="fr",
        name="Français",
        aliases=("French",),
        messages={
            "language.description": "afficher ou changer la langue de l’interface",
            "language.current": "Langue : {language}. {usage}",
        },
    )
)

raise SystemExit(main())
```

This illustrative partial pack appears in `/language` and accepts `--language fr`; untranslated keys use English. A complete bundled language should provide the same domain dictionaries as English and be registered by the built-in loader. UI and command dispatch do not need new language-specific branches. Duplicate codes or aliases are rejected rather than replacing another pack.

`register_language` adds a language; it does not patch already registered English/Chinese dictionaries. An additional pack can contain custom tool keys, using the tool's authored label/description as fallback when those keys are absent elsewhere. To extend the shipped Chinese/English editions, add matching keys to their source catalogs. Extensions without localization metadata keep their authored text.

Before shipping a catalog, inspect key parity, placeholder consistency, wheel inclusion, startup precedence, save failure, live switching and independent conversations. Exercise both full-screen and plain interactions and confirm that structured identifiers and raw content are unchanged. Use the existing end-to-end paths described in [Development and CI](testing-and-ci.md).
