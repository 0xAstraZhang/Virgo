"""Approval question bodies and the plain console's input hints."""
from __future__ import annotations

import json
import threading

import pytest

from virgo.agent.types import AgentTool, ToolContext
from virgo.ai.providers import faux
from virgo.cli.console import ConsoleBroker, TerminalUI
from virgo.i18n import Translator

from tests.support.shell import invoke


@pytest.mark.parametrize("language,read,write,network", [
    ("en", "Read access: ", "Write access: ", "Shell network: allowed"),
    ("zh-CN", "读取权限：", "写入权限：", "Shell 联网：允许"),
])
def test_grants_precede_details_with_distinct_project(broker_session, project, language, read, write, network):
    session = broker_session(preset="read-only", settings={"language": language})
    cwd = project / "subdir"
    cwd.mkdir()
    outside = project.parent / "outside"
    args = {"path": "résultat.txt", "content": "结果"}
    request = {"tool": "write", "arguments": args, "reason": "Export results",
               "permissions": {"read_paths": [str(outside)], "write_paths": [args["path"]], "network_allowed": True}}
    ctx = ToolContext("request", cwd, threading.Event(), origin=session.main.origin)
    session.tool("request_permissions").execute(request, ctx)
    assert session.ui.questions[0].splitlines() == [
        f"{read}{outside.resolve()}", f"{write}{(cwd / args['path']).resolve()}", network,
        f"Operation: write {json.dumps(args, ensure_ascii=False)}", "Reason: Export results",
        f"Working directory: {cwd.resolve()}", f"Project: {project.resolve()}",
    ]


def test_unknown_grant_displays_its_kind_and_target(broker_session):
    session = broker_session()
    ctx = ToolContext("request", session.cwd, threading.Event(), origin=session.main.origin)
    session.services["execution"].sandbox._confirm(
        ctx, session.main.execution_context, (("other_kind", "other target"),), "Requested access",
    )
    first = session.ui.questions[0].splitlines()[0]
    assert "other_kind" in first and "other target" in first


def test_external_tool_question_ends_with_host_scope(broker_session):
    session = broker_session()
    session.register_tool(AgentTool("publish", "External write", {"type": "object"}, lambda *_: "done"),
                          roles=("main",), owner="test")
    faux.script("test", faux.reply(tools=[("publish", {})]), faux.reply("done"))
    session.prompt("Request external tool")
    question, = session.ui.questions
    assert question.endswith("may modify host state.")


@pytest.mark.parametrize("language,hint", [
    ("en", "Allow for this session [y] / Deny [n] / Always allow for project [a]"),
    ("zh-CN", "本次会话允许 [y] / 拒绝 [n] / 此项目始终允许 [a]"),
])
def test_plain_resource_approval_adds_one_localized_hint(monkeypatch, language, hint):
    broker = ConsoleBroker()
    ui = TerminalUI(broker)
    ui.set_translator(Translator(language))
    prompts = []
    monkeypatch.setattr(broker, "read_value", lambda question, write: prompts.append(question) or "a")
    for question in ("Grant access?", f"Grant access?\n{hint}"):
        assert ui.confirm(question) == "a"
        assert prompts[-1].rstrip().endswith(hint)
        assert prompts[-1].count(hint) == 1


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("command,ending", [
    ("git reset --hard", "Approve executing this command once?"),
    ("echo ready", "switch permanently with /permissions no-limit.)"),
])
def test_plain_single_operation_has_one_stdin_hint(broker_session, monkeypatch, language, command, ending):
    broker = ConsoleBroker()
    session = broker_session(ui=TerminalUI(broker), settings={"language": language})
    prompts = []
    monkeypatch.setattr(broker, "read_value", lambda question, write: prompts.append(question) or "n")
    assert invoke(session, command).is_error
    prompt, = prompts
    assert prompt.endswith(ending + " [y/N] ")
    assert prompt.lower().count("[y/n]") == 1
