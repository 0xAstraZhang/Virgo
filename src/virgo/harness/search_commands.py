"""Search backend selection shared by plain, fullscreen, and headless clients."""

from __future__ import annotations

CHOICES = {'Auto — prefer model-native search; otherwise use free DuckDuckGo': 'auto',
 'DuckDuckGo — free, no API key required': 'duckduckgo',
 'DeepSeek — official search, API key required': 'deepseek',
 'Off — disable web search': 'off'}
_CHOICE_KEYS = {'Auto — prefer model-native search; otherwise use free DuckDuckGo': 'harness.search.choice.auto',
 'DeepSeek — official search, API key required': 'harness.search.choice.deepseek',
 'DuckDuckGo — free, no API key required': 'harness.search.choice.duckduckgo',
 'Off — disable web search': 'harness.search.choice.off'}
USAGE = 'Usage: /search [auto|duckduckgo|deepseek|off] (aliases: ddg, ds)'


def configure_search(session, args: str) -> None:
    service = session.services.get("search")
    if service is None:
        session.ui.print(session.t("harness.search.disabled"), "error")
        return
    choices = {session.t(_CHOICE_KEYS[label]): value for label, value in CHOICES.items()}
    usage = session.t("harness.search.usage")
    choice = args.strip().lower()
    if not choice:
        if not session.ui.interactive:
            session.ui.print(session.t("harness.search.current", selection=service.selection, usage=usage))
            return
        selected = session.ui.select(session.t("harness.search.picker", selection=service.selection), list(choices))
        if selected is None:
            session.ui.print(session.t("harness.search.kept", selection=service.selection), "dim")
            return
        choice = choices.get(selected, "")
    choice = {"ds": "deepseek", "ddg": "duckduckgo"}.get(choice, choice)
    if choice not in CHOICES.values():
        session.ui.print(usage, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        session.ui.print(session.t("harness.search.failed", error=error), "error")
        return
    session.ui.print(session.t("harness.search.saved", selection=service.selection), "ok")
