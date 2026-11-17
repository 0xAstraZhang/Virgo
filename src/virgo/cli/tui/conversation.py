"""Typed state owned by one live conversation in the fullscreen client.

A :class:`ConversationState` is the only stored copy of the per-conversation
UI state. Each ``RunController`` owns exactly one state and is the
conversation's binding to its ``AgentSession`` and ``FullscreenUI``; switching
panes therefore only selects another controller instead of copying attributes
between the application and the pane.

Only the UI thread mutates this state. Workers keep communicating through the
thread-safe :class:`~virgo.cli.tui.bridge.FullscreenUI` queues as before.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...harness.runtime import Runtime
    from .activity import RunActivity
    from .bridge import Confirmation, InputRequest
    from .editor import InputHistory


@dataclass
class ConversationState:
    """Mutable presentation and scheduling state of one conversation.

    ``activity`` carries the animation clock and run summary; ``history`` is
    the user-level input history shared by every live pane. The request queues
    hold what the bridge has not answered yet, while ``active_*`` records which
    queued request currently owns the dock.
    """

    activity: RunActivity
    history: InputHistory
    confirmations: deque[Confirmation] = field(default_factory=deque)
    active_confirmation: int | None = None
    input_requests: deque[InputRequest] = field(default_factory=deque)
    active_input: int | None = None
    input_request: InputRequest | None = None
    completion_names: list[str] = field(default_factory=list)
    completion_query: str | None = None
    completion_suppressed: str | None = None
    last_interrupt: float = 0.0
    browsing: bool = False
    task_runtime: Runtime | None = None
    deferred: deque[str] = field(default_factory=deque)
    steering: list[str] = field(default_factory=list)
