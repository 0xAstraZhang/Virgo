"""Page-reader selection uses the same UI contract as search."""

from __future__ import annotations

CHOICES = {
    "Auto — 原生网页读取优先，否则使用 HTTP": "auto",
    "HTTP — 直接读取公开网页，无需 API key": "http",
    "DeepSeek — 官方 Fetch API，需要 API key": "deepseek",
    "Off — 关闭网页读取": "off",
}
USAGE = "Usage: /fetch [auto|http|deepseek|off]"


def configure_fetch(session, args):
    service = session.services.get("fetch")
    if service is None:
        session.ui.print("Fetch is disabled by features.fetch; enable it in settings first.", "error")
        return
    choice = args.strip().lower()
    if not choice:
        if not session.ui.interactive:
            paths = getattr(session.main, "_web_paths", {})
            detail = "\nLast request paths: " + str(paths) if paths else ""
            session.ui.print(f"fetch: {service.selection}{detail}\n{USAGE}")
            return
        selected = session.ui.select(f"Page reader (current: {service.selection})", list(CHOICES))
        if selected is None:
            session.ui.print(f"Kept fetch as {service.selection}", "dim")
            return
        choice = CHOICES.get(selected, "")
    choice = {"ds": "deepseek"}.get(choice, choice)
    if choice not in CHOICES.values():
        session.ui.print(USAGE, "error")
        return
    try:
        service.set_selection(choice)
    except (ValueError, OSError) as error:
        session.ui.print(f"Fetch selection failed: {error}", "error")
        return
    session.ui.print(f"fetch: {service.selection} (saved as user default; project/runtime settings take precedence on restart)", "ok")
