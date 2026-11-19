"""Exercise persistent operation grants through the session and the permission dialog."""
from __future__ import annotations

import json
import threading

from virgo.agent.types import ToolContext
from virgo.ai.providers import faux
from virgo.harness.resource_rules import ResourceRules, operation_id

from tests.support.harness import RecordingUI, tool_call_event, tool_results


def ask(session, tool, arguments, permissions, runtime=None):
    runtime = runtime or session.main
    context = ToolContext("permission", runtime.cwd, threading.Event(), runtime.name, origin=runtime.origin)
    return session.tool("request_permissions").execute({
        "tool": tool, "arguments": arguments, "permissions": permissions,
        "reason": "Read the selected dataset and write the requested results",
    }, context)


def test_always_allow_survives_restart_and_can_be_revoked_from_no_limit(sandbox_session, project, tmp_path):
    directory = tmp_path / "approved-output"
    directory.mkdir()
    target = directory / "external.txt"
    args = {"path": str(target), "content": "result"}
    first = sandbox_session(preset="read-only", ui=RecordingUI(["a"]))
    assert not ask(first, "write", args, {"write_paths": [str(directory)]}).is_error
    assert not target.exists()
    assert str(project) in first.ui.questions[0]

    restored = sandbox_session(faux.reply(tools=[("write", args)]), faux.reply("done"), preset="read-only")
    restored.prompt("Write the exact result")
    assert not tool_results(restored)[-1].is_error and target.read_text() == "result"
    assert not restored.ui.questions
    restored.prompt("/permissions no-limit")
    restored.prompt("/permissions grants")
    assert str(directory) in restored.ui.lines[-1] and "read-only" in restored.ui.lines[-1]
    identifier = restored.ui.lines[-1].split()[0]
    restored.prompt(f"/permissions revoke {identifier}")
    assert not ResourceRules(first.paths.user).list(str(project))
    later = sandbox_session(preset="read-only", ui=RecordingUI(["n"]))
    assert ask(later, "write", args, {"write_paths": [str(directory)]}).is_error
    assert len(later.ui.questions) == 1


def test_saved_command_scope_and_incremental_resource_prompt(broker_session, project, tmp_path):
    directory = tmp_path / "testdata"
    directory.mkdir()
    session = broker_session(preset="read-only", ui=RecordingUI(["a", "y"]))
    shell = next(tool for tool in session.main.tools if tool.execution_scope == "process")
    command = {"command": "pytest -q"}
    reads = {"read_paths": [str(directory)]}
    assert not ask(session, shell.name, command, reads).is_error
    assert not ask(session, shell.name, command, reads).is_error
    assert not ask(session, shell.name, command, reads | {"write_paths": [str(directory)]}).is_error
    assert len(session.ui.questions) == 2
    assert "read_paths" in session.ui.questions[0]
    assert "write_paths" in session.ui.questions[1] and "read_paths" not in session.ui.questions[1]
    assert str(project) in session.ui.questions[1]

    for changed in ("pytest -q -x", "pytest -q && pytest"):
        assert ask(session, shell.name, {"command": changed}, reads).is_error
    child = session.create_runtime("worker", "subagent", parent=session.main, approval_policy="never")
    try:
        assert ask(session, shell.name, command, reads, runtime=child).is_error
    finally:
        session.release_runtime(child)
    other_project = tmp_path / "other"
    other_project.mkdir()
    other = broker_session(cwd=other_project, preset="read-only")
    assert ask(other, shell.name, command, reads).is_error
    session.prompt("/permissions workspace-edit")
    assert ask(session, shell.name, command, reads).is_error


def test_project_settings_cannot_supply_user_grants(broker_session, project, tmp_path):
    resource = tmp_path / "external.txt"
    resource.write_text("data")
    directory = project / ".virgo"
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({"resourceGrants": ["allow"]}))
    fake_store = ResourceRules(directory)
    session = broker_session(preset="read-only")
    event = tool_call_event(session, "read", {"path": str(resource)})
    fake_store.allow((str(project), "read-only", str(project), operation_id(event)), [("read_paths", str(resource))])
    assert ask(session, "read", {"path": str(resource)}, {"read_paths": [str(resource)]}).is_error
    assert len(session.ui.questions) == 1



def test_read_only_broker_output_leaves_the_project_untouched(broker_session, project):
    session = broker_session(preset="read-only")
    context = session.main.execution_context
    original = list(project.iterdir())
    record, stream = context.outputs.create("shell")
    with stream:
        stream.write(b"result")
    assert list(project.iterdir()) == original
    assert record._path.is_relative_to(session.paths.user)
    assert context.outputs.lookup(record.id) is record
    assert context.outputs.read(record)[0] == b"result"
    assert context.outputs.read(record, limit=3, tail=True)[:2] == (b"ult", 6)
