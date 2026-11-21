"""Page-reader selection uses the same UI contract as search."""

from __future__ import annotations

_CHOICES = (
    ("harness.fetch.choice.auto", "auto"),
    ("harness.fetch.choice.http", "http"),
    ("harness.fetch.choice.deepseek", "deepseek"),
    ("harness.fetch.choice.off", "off"),
)
_VALUES = frozenset(value for _, value in _CHOICES)


def configure_fetch(api, args):
    service = api.services.get("fetch")
    if service is None:
        api.ui.print(api.t("harness.fetch.disabled"), "error")
        return
    choices = {api.t(key): value for key, value in _CHOICES}
    usage = api.t("harness.fetch.usage")
    choice = args.strip().lower()
    if not choice:
        if not api.ui.interactive:
            runtime = api.current_runtime() if hasattr(api, "current_runtime") else None
            paths = getattr(runtime, "_web_paths", {})
            detail = "\n" + api.t("harness.fetch.paths", paths=paths) if paths else ""
            api.ui.print(api.t("harness.fetch.current", selection=service.selection, detail=detail, usage=usage))
            return
        selected = api.ui.select(api.t("harness.fetch.picker", selection=service.selection), list(choices))
        if selected is None:
            api.ui.print(api.t("harness.fetch.kept", selection=service.selection), "dim")
            return
        choice = choices.get(selected, "")
    choice = {"ds": "deepseek"}.get(choice, choice)
    if choice not in _VALUES:
        api.ui.print(usage, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        api.ui.print(api.t("harness.fetch.failed", error=error), "error")
        return
    api.ui.print(api.t("harness.fetch.saved", selection=service.selection), "ok")
