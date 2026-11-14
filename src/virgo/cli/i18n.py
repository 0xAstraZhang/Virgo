"""Resolve presentation strings from the UI or conversation that owns them."""

from __future__ import annotations

from ..i18n import Translator


def translator_for(owner=None) -> Translator:
    """Use the owning pane before the visible app, including for detached replay.

    Standalone widgets retain an English default. There is no mutable global
    locale: background conversations and worker threads keep their own language.
    """
    if isinstance(owner, Translator):
        return owner
    node = owner
    while node is not None:
        translator = getattr(node, "translator", None)
        if isinstance(translator, Translator):
            return translator
        session = getattr(node, "session", None)
        if session is None:
            session = getattr(getattr(node, "controller", None), "session", None)
        translator = getattr(session, "translator", None)
        if isinstance(translator, Translator):
            return translator
        node = getattr(node, "parent", None)
    # Modal screens are not descendants of a conversation pane.
    if getattr(owner, "is_attached", False):
        translator = getattr(getattr(owner.app, "session", None), "translator", None)
        if isinstance(translator, Translator):
            return translator
    return Translator("en")


def tr(owner, key: str, /, **values) -> str:
    return translator_for(owner)(key, **values)
