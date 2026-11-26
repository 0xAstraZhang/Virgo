"""The harness talks to the outside world only through this interface."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from ..agent.types import AgentEvent
from ..ai import resolved_effort
from ..ai.types import Model
from ..i18n import Translator

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def effort_levels(model: Model) -> tuple[str, ...]:
    if model.supported_reasoning_levels is None and model.default_reasoning_level:
        return tuple(dict.fromkeys((*EFFORT_LEVELS, model.default_reasoning_level)))
    if not model.reasoning or model.supported_reasoning_levels is None:
        return EFFORT_LEVELS
    return model.supported_reasoning_levels


@dataclass(frozen=True)
class ModelChoice:
    spec: str
    label: str
    description: str
    released_at: float | None = None
    effort_levels: tuple[str, ...] = EFFORT_LEVELS
    preferred_effort: str | None = "high"
    reasoning: bool = False

    @classmethod
    def from_model(
        cls, model: Model, description: str, requested: str | None, released_at: float | None = None
    ) -> ModelChoice:
        levels = effort_levels(model)
        effective = resolved_effort(model, requested).effective
        candidates = (effective, requested, model.automatic_effort, "high")
        preferred = next((level for level in candidates if level in levels), levels[0] if levels else requested)
        return cls(model.spec, model.id, description, released_at, levels, preferred, model.reasoning)


@dataclass(frozen=True)
class PreferenceRequest:
    kind: str
    models: tuple[ModelChoice, ...]
    model: str
    effort: str | None

    def offers(self, choice: PreferenceChoice) -> bool:
        model = next((item for item in self.models if item.spec == choice.model), None)
        if model is None or (self.kind == "effort" and choice.model != self.model):
            return False
        if self.kind == "model" and choice.effort is None:
            return True
        if not model.effort_levels:
            # Selecting the model alone must preserve the saved preference.
            return self.kind == "model" and choice.effort == model.preferred_effort
        return choice.effort in model.effort_levels


@dataclass(frozen=True)
class PreferenceChoice:
    model: str
    effort: str | None
    persist: bool


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
    supports_confirm = False
    supports_confirm_once = False

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

    def warn(self, text: str) -> None:
        self.print(text, "warn")

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

    def confirmation_handler(self, *, prefer_persistent: bool = True):
        """Return the active confirmation callable declared by this UI, or None."""
        confirm = getattr(self, "confirm", None) if self.supports_confirm else None
        once = getattr(self, "confirm_once", None) if self.supports_confirm_once else None
        confirm = confirm if callable(confirm) else None
        once = once if callable(once) else None
        if prefer_persistent:
            return confirm or once
        return once or confirm
