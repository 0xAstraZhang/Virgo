"""Search backend selection shared by plain, fullscreen, and headless clients."""

from __future__ import annotations

_CHOICES = (
    ("harness.search.choice.auto", "auto"),
    ("harness.search.choice.duckduckgo", "duckduckgo"),
    ("harness.search.choice.deepseek", "deepseek"),
    ("harness.search.choice.off", "off"),
)
_VALUES = frozenset(value for _, value in _CHOICES)


def configure_search(api, args: str) -> None:
    service = api.services.get("search")
    if service is None:
        api.ui.print(api.t("harness.search.disabled"), "error")
        return
    choices = {api.t(key): value for key, value in _CHOICES}
    usage = api.t("harness.search.usage")
    choice = args.strip().lower()
    if not choice:
        if not api.ui.interactive:
            api.ui.print(api.t("harness.search.current", selection=service.selection, usage=usage))
            return
        selected = api.ui.select(api.t("harness.search.picker", selection=service.selection), list(choices))
        if selected is None:
            api.ui.print(api.t("harness.search.kept", selection=service.selection), "dim")
            return
        choice = choices.get(selected, "")
    choice = {"ds": "deepseek", "ddg": "duckduckgo"}.get(choice, choice)
    if choice not in _VALUES:
        api.ui.print(usage, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        api.ui.print(api.t("harness.search.failed", error=error), "error")
        return
    api.ui.print(api.t("harness.search.saved", selection=service.selection), "ok")
