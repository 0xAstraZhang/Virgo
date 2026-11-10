"""The harness talks to the outside world only through this interface."""

from __future__ import annotations

from ..agent.types import AgentEvent


class UI:
    """Headless default: prints nothing, answers no questions."""

    interactive = False

    def on_event(self, ev: AgentEvent) -> None:  # streaming output, tool calls, ...
        pass

    def print(self, text: str, style: str | None = None) -> None:
        pass

    def confirm(self, question: str, agent: str = "main") -> str | None:
        """Ask the user. Return 'y', 'n' or 'a' (always), or None when nobody can answer."""
        return None

    def wake(self) -> None:
        """Called when a notification arrives while the session is idle."""
        pass
