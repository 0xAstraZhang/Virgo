"""Check the actual request surface without freezing the wording of the base prompt."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import Runtime

from tests.support.harness import RecordingUI

MAIN_TOOLS = {
    "present_file",
    "read", "write", "edit", "glob", "grep", "read_output", "web_search", "web_fetch",
    "ask_user", "agent", "bash", "job_output", "job_kill", "diagnostic_get", "todo_write",
    "session_info",
}


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


def test_session_read_grant_keeps_mode_defaults_in_request_prefix(broker_session, project):
    approved = project.parent / "outside-approved.txt"
    sibling = project.parent / "outside-sibling.txt"
    content = "approved text\n"
    approved.write_bytes(content.encode())
    sibling.write_text("unapproved sibling content")
    ui = RecordingUI(["y", "n"])
    session = broker_session(preset="workspace-edit", ui=ui)
    read = faux.reply(tools=[("read", {"path": str(approved)})])
    replies = iter([read, faux.reply("done"), read,
                    faux.reply(tools=[("read", {"path": str(sibling)})]), faux.reply("done")])
    requests = []

    def respond(context):
        requests.append(context)
        return next(replies)

    faux.handler("test", respond)
    session.prompt("Read the external file")
    session.prompt("Read it again, then read its sibling")

    first, after_read, _, after_reread, after_decline = requests
    for request in (after_read, after_reread):
        result = request.messages[-1]
        assert not result.is_error and result.text() == f"1|{content}"
    denied = after_decline.messages[-1]
    assert denied.is_error and denied.details["boundary"]["reason"] == "user_declined"
    assert "unapproved sibling content" not in denied.text()
    assert len(ui.questions) == 2
    assert f"Read access: {approved.resolve()}" in ui.questions[0].splitlines()
    assert f"Read access: {sibling.resolve()}" in ui.questions[1].splitlines()

    assert all(request.system == first.system for request in requests)
    tool_schemas = {json.dumps([asdict(tool) for tool in request.tools]) for request in requests}
    assert len(tool_schemas) == 1
    boundary = after_read.system.split("<execution_boundary>")[1].split("</execution_boundary>")[0]
    descriptor, guidance = boundary.split("\n", 1)
    facts = json.loads(descriptor)
    assert "effective" not in facts and "requested" not in facts
    assert facts["mode_defaults"]["read_paths"] == facts["mode_defaults"]["write_paths"] == [str(project.resolve())]
    assert "mode defaults before user grants" in guidance
    assert "File tools check access for each call and return data after any required approval" in guidance


@pytest.mark.parametrize("preset", ["workspace-edit", "read-only"])
@pytest.mark.parametrize("windows", [False, True], ids=["posix", "native-windows"])
def test_sandbox_temp_guidance_in_request_prefix(broker_session, monkeypatch, preset, windows):
    runtime = broker_session(preset=preset).main
    monkeypatch.setattr("virgo.execution_policy.is_native_windows", lambda: windows)

    first = runtime.request_context([])
    second = runtime.request_context([])
    assert (first.system, first.tools) == (second.system, second.tools)
    boundary = first.system.split("<execution_boundary>")[1].split("</execution_boundary>")[0]
    # The JSON descriptor can contain host paths such as /tmp on the machine running the test.
    _, guidance = boundary.split("\n", 1)
    assert "$TMPDIR" in guidance and "removed when the command finishes" in guidance
    assert "writable workspace" in guidance and "persist across calls" in guidance
    assert "clean them up" in guidance
    if windows:
        assert "$env:TMPDIR" in guidance
        assert "$env:TEMP" in guidance and "%TEMP%" in guidance and "session-scoped" in guidance
        assert "outside write_paths" in guidance
        assert "/tmp" not in guidance
    else:
        assert "/tmp is read-only by default" in guidance
        assert "do not write to /tmp" in guidance
        assert "outside write_paths" in guidance
        assert "%TEMP%" not in guidance


def test_no_limit_boundary_omits_sandbox_temp_guidance(broker_session):
    system = broker_session(preset="no-limit").main.request_context([]).system
    boundary = system.split("<execution_boundary>")[1].split("</execution_boundary>")[0]
    assert "Host" in boundary
    for claim in (
        "/tmp", "TMPDIR", "%TEMP%", "$env:TEMP", "when the command finishes",
        "Mach/XPC", "bubblewrap", "127.0.0.1", "AppContainer",
    ):
        assert claim not in boundary


@pytest.mark.parametrize("backend", ["seatbelt", "bubblewrap", "windows-appcontainer"])
def test_sandbox_backend_service_guidance_in_request_prefix(broker_session, monkeypatch, backend):
    session = broker_session(preset="workspace-edit")
    runtime = session.main
    monkeypatch.setattr("virgo.execution_policy.is_native_windows", lambda: backend == "windows-appcontainer")
    monkeypatch.setattr(type(runtime.execution_context.executor), "backend", property(lambda _self: backend))

    boundary = runtime.request_context([]).system.split("<execution_boundary>")[1].split("</execution_boundary>")[0]
    _, guidance = boundary.split("\n", 1)
    if backend == "seatbelt":
        assert "Mach/XPC" in guidance and "mdls" in guidance and "Keychain" in guidance
        assert "local files directly" in guidance
        assert "/run" not in guidance and "127.0.0.1" not in guidance
    elif backend == "bubblewrap":
        assert "/run" in guidance and "D-Bus" in guidance and "systemd" in guidance and "docker.sock" in guidance
        assert "Mach/XPC" not in guidance and "127.0.0.1" not in guidance
    else:
        assert "AppContainer" in guidance and "127.0.0.1" in guidance and "[::1]" in guidance
        assert "user-profile" in guidance
        assert "/tmp" not in guidance and "/run" not in guidance and "Mach/XPC" not in guidance


@pytest.mark.parametrize(("ui_language", "guidance"), [
    ("en", "language of the user's message"),
    ("zh-CN", "用户当前消息的语言"),
], ids=["en", "zh-CN"])
def test_response_language_follows_user_message_in_each_ui_locale(make_session, ui_language, guidance):
    session = make_session(faux.reply("ok"), settings={"language": ui_language})
    session.prompt("¿Qué hace este proyecto?")
    request = faux.CALLS["test"][-1]
    language = request["system"].split("<language>")[1].split("</language>")[0]
    assert guidance in language


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


def test_permission_switch_updates_requests_and_restores_the_original_prefix(broker_session):
    session = broker_session()
    sandbox = session.services["execution"]
    original_mode = sandbox.mode_for(session.main)
    original = session.main.request_context([])
    sandbox.select_mode("read-only")
    restricted = session.main.request_context([])
    assert restricted.tools == original.tools
    assert '"write_paths": []' in restricted.system
    sandbox.select_mode(original_mode.id)
    restored = session.main.request_context([])
    assert '"write_paths": [' in restored.system
    assert restored.tools == original.tools


def test_environment_includes_git_branch_and_bounded_top_level_entries(broker_session, project):
    git_dir = project / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/feat/info-gathering\n", encoding="utf-8")
    (project / ".gitignore").write_text("ignored_dir/\n*.tmp\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "ignored_dir").mkdir()
    (project / "node_modules").mkdir()
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    (project / "scratch.tmp").write_text("skip\n", encoding="utf-8")

    session = broker_session(preset="workspace-edit")
    env = session.main.request_context([]).system.split("<environment>")[1].split("</environment>")[0]
    assert "Git branch: feat/info-gathering" in env
    assert "Top-level entries: src/, .gitignore, README.md" in env
    assert "ignored_dir" not in env
    assert "node_modules" not in env
    assert "scratch.tmp" not in env


def test_environment_git_detached_head_and_sandboxed_external_worktree_fail_closed(broker_session, project, tmp_path):
    # Detached HEAD inside workspace is reported concisely.
    git_dir = project / ".git"
    git_dir.mkdir()
    sha = "a1b2c3d4e5f6" + "0" * 28
    (git_dir / "HEAD").write_text(f"{sha}\n", encoding="utf-8")
    env = broker_session(preset="workspace-edit").main.request_context([]).system
    assert "Git branch: detached at a1b2c3d4e5f6" in env

    # External worktree gitdir outside workspace must not be read under sandboxed policy.
    external_git = tmp_path / "external-gitdir"
    external_git.mkdir()
    (external_git / "HEAD").write_text("ref: refs/heads/secret-branch\n", encoding="utf-8")
    (git_dir / "HEAD").unlink()
    git_dir.rmdir()
    (project / ".git").write_text(f"gitdir: {external_git}\n", encoding="utf-8")

    sandbox_env = broker_session(preset="workspace-edit").main.request_context([]).system
    assert "secret-branch" not in sandbox_env
    host_env = broker_session(preset="no-limit").main.request_context([]).system
    assert "Git branch: secret-branch" in host_env


def test_environment_caps_top_level_entries_and_hides_denied_paths(broker_session, project):
    from virgo.harness.prompt import _workspace_entries

    from tests.support.execution import file_policy

    for idx in range(35):
        (project / f"file_{idx:02d}.txt").write_text("x", encoding="utf-8")
    env = broker_session(preset="workspace-edit").main.request_context([]).system.split("<environment>")[1].split(
        "</environment>"
    )[0]
    assert "(+5 more)" in env

    private_dir = project / "private_vault"
    private_dir.mkdir()
    (private_dir / "secret.json").write_text("{}", encoding="utf-8")
    policy = file_policy(project, private_paths=(private_dir,))
    entries = _workspace_entries(project, project.resolve(), policy)
    assert entries is not None and "private_vault" not in entries


def test_environment_hides_enclosed_private_virgo_dir_on_posix(make_session, tmp_path, monkeypatch):
    import os

    if os.name != "posix":
        pytest.skip("POSIX $HOME workspace")
    fake_home = tmp_path / "home-ws"
    fake_home.mkdir()
    private_dir = fake_home / ".virgo"
    private_dir.mkdir(mode=0o700)
    (private_dir / "auth.json").write_text("{}", encoding="utf-8")
    (fake_home / "notes.txt").write_text("x", encoding="utf-8")

    monkeypatch.setenv("VIRGO_HOME", str(private_dir))
    session = make_session(
        cwd=fake_home,
        home=private_dir,
        settings={"execution": {"mode": "workspace-edit"}},
        extensions=False,
        refresh_models=False,
    )
    try:
        env = session.main.request_context([]).system.split("<environment>")[1].split("</environment>")[0]
        assert ".virgo" not in env
        assert "notes.txt" in env
    finally:
        session.shutdown()
