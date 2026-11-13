"""The harness talks to the outside world only through this interface."""

from __future__ import annotations

from ..agent.types import AgentEvent
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

    def on_event(self, ev: AgentEvent) -> None:  # streaming output, tool calls, ...
        pass

    def print(self, text: str, style: str | None = None) -> None:
        pass

    def command_result(self, text: str) -> None:
        """Explicit command output, also visible in noninteractive clients."""
        self.print(text)

    def confirm(self, question: str, agent: str = "main") -> str | None:
        """Ask the user. Return 'y', 'n' or 'a' (always), or None when nobody can answer."""
        return None

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

    def select_details(self, question: str, options: list[str], details: str) -> str | None:
        """Choose an action alongside complete, scrollable reference text."""
        self.print(details)
        return self.select(question, options)

    def input_multiline(self, question: str, *, default: str = "") -> str | None:
        """Read task content without putting it in conversation/input history."""
        if not self.interactive:
            return None
        if default:
            self.print(default)
        self.print(question + "\nFinish with a line containing .; /cancel cancels; . alone keeps existing content.")
        lines = []
        while True:
            line = self.input("│")
            if line is None or line == "/cancel":
                return None
            if line == ".":
                return "\n".join(lines) if lines else default
            lines.append(line)

    def cancel_input_timeouts(self) -> None:
        """Keep currently displayed questions open after the user joins an unattended run."""
        pass

    def wake(self) -> None:
        """Called when a notification arrives while the session is idle."""
        pass
