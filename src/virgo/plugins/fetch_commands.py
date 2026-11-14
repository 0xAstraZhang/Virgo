"""Page-reader selection uses the same UI contract as search."""

from __future__ import annotations

CHOICES = {'Auto — prefer native page reading; otherwise use HTTP': 'auto',
 'HTTP — read public pages directly, no API key required': 'http',
 'DeepSeek — official Fetch API, API key required': 'deepseek',
 'Off — disable page reading': 'off'}
_CHOICE_KEYS = {'Auto — prefer native page reading; otherwise use HTTP': 'harness.fetch.choice.auto',
 'DeepSeek — official Fetch API, API key required': 'harness.fetch.choice.deepseek',
 'HTTP — read public pages directly, no API key required': 'harness.fetch.choice.http',
 'Off — disable page reading': 'harness.fetch.choice.off'}
USAGE = 'Usage: /fetch [auto|http|deepseek|off]'


def configure_fetch(api, args):
    service = api.services.get("fetch")
    if service is None:
        api.ui.print(api.t("harness.fetch.disabled"), "error")
        return
    choices = {api.t(_CHOICE_KEYS[label]): value for label, value in CHOICES.items()}
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
    if choice not in CHOICES.values():
        api.ui.print(usage, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        api.ui.print(api.t("harness.fetch.failed", error=error), "error")
        return
    api.ui.print(api.t("harness.fetch.saved", selection=service.selection), "ok")
