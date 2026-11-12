"""Drive selected shell tools through a real session and the faux model."""
from __future__ import annotations

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage


def selected(session):
    return session.services["shell"].current


def result(session):
    return next(m for m in reversed(session.main.messages) if isinstance(m, ToolResultMessage))


def invoke(session, command, **kwargs):
    faux.push("test", faux.reply(tools=[(selected(session).tool_name, {"command": command, **kwargs})]), faux.reply("done"))
    session.prompt("Run the requested command")
    return result(session)
