"""Search backend selection shared by plain, fullscreen, and headless clients."""

from __future__ import annotations

CHOICES = {
    "Auto — 模型原生搜索优先，否则使用免费的 DuckDuckGo": "auto",
    "DuckDuckGo — 免费，无需 API key": "duckduckgo",
    "DeepSeek — 官方搜索，需要 API key": "deepseek",
    "Off — 关闭联网搜索": "off",
}
USAGE = "Usage: /search [auto|duckduckgo|deepseek|off] (aliases: ddg, ds)"


def configure_search(session, args: str) -> None:
    service = session.services.get("search")
    if service is None:
        session.ui.print("Search is disabled by features.search; enable it in settings to choose a backend.", "error")
        return
    choice = args.strip().lower()
    if not choice:
        if not session.ui.interactive:
            session.ui.print(f"search: {service.selection}\n{USAGE}")
            return
        selected = session.ui.select(f"Search backend (current: {service.selection})", list(CHOICES))
        if selected is None:
            session.ui.print(f"Kept search as {service.selection}", "dim")
            return
        choice = CHOICES.get(selected, "")
    choice = {"ds": "deepseek", "ddg": "duckduckgo"}.get(choice, choice)
    if choice not in CHOICES.values():
        session.ui.print(USAGE, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        session.ui.print(f"Search selection failed: {error}", "error")
        return
    session.ui.print(f"search: {service.selection} (saved as user default; project/runtime settings take precedence on restart)", "ok")
