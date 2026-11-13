"""The language command shares the language registry with clients and extensions."""

from __future__ import annotations

from ..i18n import available_languages, normalize_language


def configure_language(session, args: str) -> None:
    names = "|".join(pack.name for pack in available_languages())
    usage = session.t("language.usage", languages=names)
    value = args.strip()
    if not value:
        session.ui.command_result(
            session.t("language.current", language=session.translator.name, usage=usage)
        )
        return
    try:
        code = normalize_language(value)
    except ValueError:
        session.ui.print(session.t("language.invalid", value=value, usage=usage), "error")
        return
    if session.busy:
        session.ui.print(session.t("language.busy"), "error")
        return
    try:
        session.set_language(code)
    except (OSError, ValueError) as error:
        session.ui.print(session.t("language.save_failed", error=error), "error")
        return
    session.ui.command_result(session.t("language.changed", language=session.translator.name))
