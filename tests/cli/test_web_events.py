"""Native stream -> console/JSON. Failure modes: missing URL, wrong action, lost error, binary dumps."""

import json

from virgo.agent.types import AgentEvent
from virgo.ai.stream import StreamEvent
from virgo.ai.types import AssistantMessage, RawContent
from virgo.cli.console import PrintUI, TerminalUI

from tests.support.harness import ScriptedBroker


def test_native_fetch_event_reaches_plain_console_and_json(capsys):
    block = RawContent("openai-responses", {"type": "web_search_call", "id": "read", "status": "completed",
        "action": {"type": "open_page", "url": "https://example.com/page"}})
    ev = AgentEvent("message_update", stream_event=StreamEvent("native_tool_end", AssistantMessage([block]), 0))
    ui = TerminalUI(ScriptedBroker())
    output = []
    ui.write = lambda text, *args: output.append(text)
    ui.on_event(ev)
    assert "web_fetch" in "".join(output) and "https://example.com/page" in "".join(output)
    PrintUI(json_mode=True).on_event(ev)
    data = json.loads(capsys.readouterr().out)
    assert data["native"]["name"] == "web_fetch"
    assert data["native"]["status"] == "completed"
    assert data["native"]["args"]["url"] == "https://example.com/page"
