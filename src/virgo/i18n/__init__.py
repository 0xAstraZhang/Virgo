"""Session-scoped translations and the public language-pack registration API.

Only human-readable text is translated. Command names, schema keys, enum values,
stored events, user content and third-party responses remain unchanged.
"""

from __future__ import annotations

import copy
import importlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from string import Formatter
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.types import AgentTool

DEFAULT_LANGUAGE = "en"
_DOMAINS = ("core", "harness", "ui", "features", "tools")
_packs: dict[str, LanguagePack] = {}
_aliases: dict[str, str] = {}
_registry_lock = threading.RLock()


@dataclass(frozen=True)
class LanguagePack:
    """A language's canonical code, native display name and keyed messages.

    Missing messages fall back to English. Aliases are case-insensitive and
    accept underscores in place of hyphens. Register packs before sessions start.
    """

    code: str
    name: str
    messages: Mapping[str, str]
    aliases: tuple[str, ...] = ()


def _alias(value: str) -> str:
    return value.strip().replace("_", "-").casefold()


def _fields(message: str) -> set[str]:
    fields: set[str] = set()
    for _, field, spec, conversion in Formatter().parse(message):
        if conversion not in (None, "s", "r", "a"):
            raise ValueError("Translation placeholders use only !s, !r or !a conversions")
        if field is not None:
            if not field or not field.isidentifier():
                raise ValueError("Translation placeholders must be named identifiers")
            fields.add(field)
        if spec:
            fields.update(_fields(spec))
    return fields


def _register(pack: LanguagePack) -> None:
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*", pack.code) or not pack.name.strip():
        raise ValueError("A language pack needs a language code and a display name")
    if pack.code in _packs:
        raise ValueError(f"Language already registered: {pack.code}")
    aliases = {_alias(value) for value in (pack.code, pack.name, *pack.aliases)}
    if "" in aliases or any(alias in _aliases for alias in aliases):
        raise ValueError(f"Language alias is empty or already registered: {pack.code}")
    messages = dict(pack.messages)
    fallback = _packs.get(DEFAULT_LANGUAGE)
    for key, message in messages.items():
        if not isinstance(key, str) or not key or not isinstance(message, str):
            raise ValueError("Translation keys and values must be strings")
        fields = _fields(message)
        if fallback and key in fallback.messages and fields != _fields(fallback.messages[key]):
            raise ValueError(f"Translation placeholders do not match English: {pack.code}/{key}")
    frozen = LanguagePack(pack.code, pack.name, MappingProxyType(messages), tuple(pack.aliases))
    _packs[pack.code] = frozen
    _aliases.update({alias: pack.code for alias in aliases})


def _load_messages(locale: str) -> dict[str, str]:
    messages: dict[str, str] = {}
    for domain in _DOMAINS:
        entries = importlib.import_module(f"{__name__}.locales.{locale}.{domain}").MESSAGES
        duplicate = messages.keys() & entries.keys()
        if duplicate:
            raise ValueError(f"Duplicate translation keys: {sorted(duplicate)}")
        messages.update(entries)
    return messages


def _ensure_builtins() -> None:
    with _registry_lock:
        if not _packs:
            english, chinese = _load_messages("en"), _load_messages("zh_CN")
            try:
                _register(LanguagePack("en", "English", english, ("en-US", "en-GB")))
                _register(LanguagePack("zh-CN", "中文", chinese, ("zh", "Chinese", "简体中文")))
            except Exception:
                _packs.clear()
                _aliases.clear()
                raise


def register_language(pack: LanguagePack) -> None:
    """Register an additional language without changing UI or command dispatch."""
    _ensure_builtins()
    with _registry_lock:
        _register(pack)


def available_languages() -> tuple[LanguagePack, ...]:
    _ensure_builtins()
    return tuple(_packs.values())


def normalize_language(value: str) -> str:
    _ensure_builtins()
    code = _aliases.get(_alias(value)) if isinstance(value, str) else None
    if code is None:
        raise ValueError(f"Unsupported language: {value}")
    return code


class Translator:
    """A mutable language selection owned by one session, never global state."""

    def __init__(self, language: str = DEFAULT_LANGUAGE):
        self._language = normalize_language(language)

    @property
    def language(self) -> str:
        return self._language

    @property
    def name(self) -> str:
        return _packs[self._language].name

    def set_language(self, language: str) -> None:
        self._language = normalize_language(language)

    def has(self, key: str) -> bool:
        return key in _packs[self._language].messages or key in _packs[DEFAULT_LANGUAGE].messages

    def __call__(self, key: str, /, **values: object) -> str:
        fallback = _packs[DEFAULT_LANGUAGE].messages.get(key, key)
        message = _packs[self._language].messages.get(key, fallback)
        try:
            return message.format_map(values)
        except (KeyError, ValueError, TypeError):
            # A broken translation must not break a working English interface.
            # Invalid arguments still surface when the fallback cannot format.
            if message == fallback:
                raise
            return fallback.format_map(values)

    text = __call__


def localize_tool(tool: AgentTool, translator: Translator) -> AgentTool:
    """Copy declared tool metadata, leaving shared tools and schema identifiers intact."""
    source = getattr(tool, "_i18n_source", tool)
    prefix = source.i18n_key
    if not prefix:
        return source
    result = copy.copy(source)
    result._i18n_source = source
    english = Translator(DEFAULT_LANGUAGE)
    values = source.i18n_values(translator) if callable(source.i18n_values) else source.i18n_values
    default_values = source.i18n_values(english) if callable(source.i18n_values) else source.i18n_values
    for field in ("label", "description"):
        key = f"{prefix}.{field}"
        # An extension may use dataclasses.replace on a builtin and override its
        # prose while retaining i18n_key. Preserve that explicitly authored text.
        if translator.has(key) and (
            not english.has(key) or getattr(source, field) == english(key, **default_values)
        ):
            setattr(result, field, translator(key, **values))

    def schema(value, path):
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        out = copy.deepcopy(value)
        for name in ("description", "title"):
            item, key = value.get(name), f"{path}.{name}"
            if (
                isinstance(item, str)
                and translator.has(key)
                and (not english.has(key) or item == english(key))
            ):
                out[name] = translator(key)
        # Traverse schemas, never example/default/enum payloads that happen to
        # contain properties called "description" or "title".
        for name in (
            "properties",
            "patternProperties",
            "$defs",
            "definitions",
            "dependentSchemas",
            "dependencies",
        ):
            if isinstance(value.get(name), dict):
                out[name] = {key: schema(item, f"{path}.{name}.{key}") for key, item in value[name].items()}
        for name in (
            "items",
            "additionalItems",
            "additionalProperties",
            "unevaluatedItems",
            "unevaluatedProperties",
            "contains",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
            "contentSchema",
        ):
            if name in value:
                item = value[name]
                out[name] = (
                    [schema(child, f"{path}.{name}.{index}") for index, child in enumerate(item)]
                    if name == "items" and isinstance(item, list)
                    else schema(item, f"{path}.{name}")
                )
        for name in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if isinstance(value.get(name), list):
                out[name] = [schema(item, f"{path}.{name}.{index}") for index, item in enumerate(value[name])]
        return out

    result.parameters = schema(source.parameters, f"{prefix}.parameters")
    return result
