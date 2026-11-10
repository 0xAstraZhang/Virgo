"""The harness talks to the outside world only through this interface."""

from __future__ import annotations

from ..agent.types import AgentEvent
from .preferences import PreferenceChoice, PreferenceRequest


class UI:
    """Headless default: prints nothing, answers no questions."""

    interactive = False
    supports_preferences = False

    def on_event(self, ev: AgentEvent) -> None:  # streaming output, tool calls, ...
        pass

    def print(self, text: str, style: str | None = None) -> None:
        pass

    def confirm(self, question: str, agent: str = "main") -> str | None:
        """Ask the user. Return 'y', 'n' or 'a' (always), or None when nobody can answer."""
        return None

    def select(self, question: str, options: list[str]) -> str | None:
        """Choose one label, or return None when cancelled or noninteractive."""
        return None

    def input(self, question: str, *, password: bool = False, default: str = "") -> str | None:
        """Read a value without adding it to chat history. Passwords stay hidden."""
        return None

    def choose_preferences(self, request: PreferenceRequest) -> PreferenceChoice | None:
        """Choose model/effort settings, or cancel when this client has no picker."""
        return None

    def wake(self) -> None:
        """Called when a notification arrives while the session is idle."""
        pass
