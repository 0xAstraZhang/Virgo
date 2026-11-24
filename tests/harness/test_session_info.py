import json
import threading

from virgo.agent.types import ToolContext
from virgo.ai.providers import faux

from tests.support.harness import tool_results


def lookup(session):
    faux.push("test", faux.reply(tools=[("session_info", {})]), faux.reply("Done"))
    session.prompt("把此对话轨迹的文件位置发我")
    result, = tool_results(session)
    assert result.tool_name == "session_info" and not result.is_error, result.text()
    info = json.loads(result.text())
    session.commands.get("session").fn(session, "")
    assert info["session_id"] in session.ui.lines[-1]
    assert (info["transcript_path"] or session.t("harness.session.memory")) in session.ui.lines[-1]
    return info


def test_session_info_matches_current_transcript_after_switch(make_session):
    session = make_session(persist=True)
    first = lookup(session)
    assert first == {"session_id": session.store.id, "transcript_path": str(session.store.path)}
    session.new_session()
    second = lookup(session)
    assert second == {"session_id": session.store.id, "transcript_path": str(session.store.path)}
    assert first != second
    assert session.tool("session_info", role="subagent") is None


def test_memory_only_session_has_no_transcript_path(make_session):
    session = make_session()
    assert lookup(session) == {"session_id": session.store.id, "transcript_path": None}


def test_unbound_query_cannot_borrow_the_selected_session(make_session):
    session = make_session(persist=True)
    ctx = ToolContext("unbound", session.cwd, threading.Event())
    result = session.tool("session_info").execute({}, ctx)
    assert result.is_error
    assert session.store.id not in result.as_text()
