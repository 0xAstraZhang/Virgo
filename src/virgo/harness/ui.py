"""The harness talks to the outside world only through this interface."""

from __future__ import annotations

from contextlib import contextmanager

from ..agent.types import AgentEvent
from ..i18n import Translator
from .preferences import PreferenceChoice, PreferenceRequest


class _TimedOut:
    """Returned by ``select``/``input`` when nobody answered before ``timeout``."""

    def __repr__(self) -> str:
        return "TIMED_OUT"

    def __bool__(self) -> bool:
        return False


TIMED_OUT = _TimedOut()


class UI:
    """Headless default: prints nothing, answers no questions."""

    interactive = False
    supports_preferences = False

    def set_translator(self, translator: Translator) -> None:
        self.translator = translator

    def t(self, key: str, /, **values: object) -> str:
        return (getattr(self, "translator", None) or Translator())(key, **values)

    def language_changed(self) -> None:
        """Refresh client chrome after this UI's session changes language."""
        pass

    def on_event(self, ev: AgentEvent) -> None:  # streaming output, tool calls, ...
        pass

    def print(self, text: str, style: str | None = None) -> None:
        pass

    def save_failed(self, session_id: str, path: str, message: str) -> None:
        self.print(message, "error")

    def command_result(self, text: str) -> None:
        """Explicit command output, also visible in noninteractive clients."""
        self.print(text)

    def command_begin(self) -> None:
        """Mark the start of synchronous slash-command output."""

    def command_end(self) -> None:
        """Mark the end of synchronous slash-command output."""

    @contextmanager
    def command_scope(self):
        """Route output emitted by a slash command through the command channel."""
        self.command_begin()
        try:
            yield
        finally:
            self.command_end()

    @contextmanager
    def command_suspended(self):
        """Suspend command routing while a command starts a normal agent run."""
        yield

    def confirm(self, question: str, agent: str = "main") -> str | None:
        """Ask the user. Return 'y', 'n' or 'a' (always), or None when nobody can answer."""
        return None

    def confirm_once(self, question: str, agent: str = "main") -> str | None:
        """Confirm one operation; the resource provider accepts only an explicit 'y'."""
        return self.confirm(question, agent)

    def select(self, question: str, options: list[str], *, timeout: float | None = None) -> str | None:
        """Choose one label, or return None when cancelled or noninteractive.

        With ``timeout`` (seconds), the question is withdrawn and ``TIMED_OUT`` is returned when
        nobody answers in time.
        """
        return None

    def input(
        self, question: str, *, password: bool = False, default: str = "", timeout: float | None = None
    ) -> str | None:
        """Read a value without adding it to chat history. Passwords stay hidden.

        ``timeout`` works as for ``select``.
        """
        return None

    def choose_preferences(self, request: PreferenceRequest) -> PreferenceChoice | None:
        """Choose model/effort settings, or cancel when this client has no picker."""
        return None

    def cancel_input_timeouts(self) -> None:
        """Keep currently displayed questions open after the user joins an unattended run."""
        pass

    def wake(self) -> None:
        """Called when a notification arrives while the session is idle."""
        pass


def confirm_callable(ui, *, prefer_persistent: bool = True):
    """Return the active confirmation callable if ``ui`` overrides confirm/confirm_once."""
    confirm = getattr(ui, "confirm", None)
    has_confirm = callable(confirm) and getattr(confirm, "__func__", None) is not UI.confirm
    once = getattr(ui, "confirm_once", None)
    has_once = callable(once) and getattr(once, "__func__", None) is not UI.confirm_once
    if prefer_persistent:
        return confirm if has_confirm else (once if has_once else None)
    return once if has_once else (confirm if has_confirm else None)
