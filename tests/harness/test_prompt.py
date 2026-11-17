"""Check the actual request surface without freezing the wording of the base prompt."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import Runtime

MAIN_TOOLS = {
    "read", "write", "edit", "glob", "grep", "web_search", "web_fetch", "todo_write", "ask_user", "agent",
    "bash", "job_output", "job_kill", "diagnostic_get", "git_read", "propose_plan", "request_permissions",
}


@pytest.mark.parametrize("location,name,permission", [
    ("home", "AGENTS.md", "disabled"),
    ("home", "SYSTEM.md", "disabled"),
    ("home", "APPEND_SYSTEM.md", "disabled"),
    ("project", "SYSTEM.md", "workspace-write"),
    ("project", "APPEND_SYSTEM.md", "disabled"),
])
def test_invalid_instruction_encoding_identifies_file(make_session, project, tmp_path, location, name, permission):
    directory = tmp_path / "home" if location == "home" else project / ".virgo"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(b"invalid instruction: \xff")
    with pytest.raises(UnicodeDecodeError) as caught:
        make_session(settings={"sandbox": {"mode": permission}})
    assert str(path) in str(caught.value)


def test_default_request_keeps_tool_definitions_without_duplicate_or_empty_sections(make_session):
    seen = []

    def respond(context):
        seen.append(context)
        return faux.reply("done")

    session = make_session(respond, respond)
    session.prompt("first")
    session.prompt("second")
    first, second = seen
    shell = session.services["shell"].current
    assert shell is not None
    assert {t.name for t in first.tools} == MAIN_TOOLS - {"bash"} | {shell.tool_name}
    assert all(t.description and t.parameters["type"] == "object" for t in first.tools)
    assert first.system == second.system
    assert [asdict(t) for t in first.tools] == [asdict(t) for t in second.tools]
    for tag in ("tools", "asking", "memory", "skills", "workflows"):
        assert f"<{tag}>" not in first.system
    assert str(session.cwd) in first.system and "<environment>" in first.system


def test_user_system_and_project_context_survive_for_their_roles(make_session, project, tmp_path):
    user = tmp_path / "home"
    config = project / ".virgo"
    config.mkdir()
    (user / "SYSTEM.md").write_text("user identity", encoding="utf-8")
    (config / "SYSTEM.md").write_text("project identity", encoding="utf-8")
    (user / "APPEND_SYSTEM.md").write_text("user appendix", encoding="utf-8")
    (config / "APPEND_SYSTEM.md").write_text("project appendix", encoding="utf-8")
    (user / "AGENTS.md").write_text("user context", encoding="utf-8")
    (project / "AGENTS.md").write_text("project context", encoding="utf-8")
    session = make_session()
    for role in ("main", "subagent", "custom"):
        runtime = session.main if role == "main" else Runtime(session, role, role)
        context = runtime.request_context([])
        assert ("project identity" in context.system) == (role == "main")
        assert "user identity" not in context.system and "user appendix" not in context.system
        for text in ("project appendix", "user context", "project context"):
            assert text in context.system
        if role != "main":
            assert not {"agent", "ask_user", "memory", "todo_write", "team_spawn"} & {t.name for t in context.tools}


def test_permission_switch_updates_requests_and_restores_the_original_prefix(make_session):
    session = make_session()
    sandbox = session.services["sandbox"]
    original_mode = sandbox.mode_for(session.main)
    original = session.main.request_context([])
    session.services["permissions"].set_work_mode("plan")
    planning = session.main.request_context([])
    assert {"read", "ask_user", "propose_plan", "git_read"} <= {t.name for t in planning.tools}
    assert planning.tools == original.tools
    assert sandbox.mode_for(session.main) == original_mode
    assert "<work_mode>" in planning.system
    sandbox.select_mode("read-only")
    restricted = session.main.request_context([])
    assert restricted.tools == original.tools
    assert '"read_only": true' in restricted.system and "<work_mode>" in restricted.system
    session.services["permissions"].set_work_mode("default")
    assert sandbox.mode_for(session.main).read_only
    sandbox.select_mode(original_mode.id)
    restored = session.main.request_context([])
    assert restored.system == original.system
    assert restored.tools == original.tools
