"""Verification tests for v2 PermissionPolicy, Broker file tools, and native guardrails."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.execution import ExecutionUnavailable
from virgo.harness.resource_rules import LEGACY_GRANTS_FILENAME
from virgo.sandbox.platform import NativePolicy
from virgo.tools.fs import glob_match

from tests.support.harness import RecordingUI, tool_results
from tests.support.sandbox import native_session, python_command, run
from tests.support.shell import selected


@pytest.fixture
def native(make_session):
    return native_session(make_session, {"linux", "darwin"})


def test_ver01_workspace_edit_allows_git_and_docs_while_confirming_virgo_and_shielding_credentials(native, project):
    """VER-01: Workspace-Edit opens .git/AGENTS.md/CLAUDE.md, confirms .virgo writes, and hard-denies credentials."""
    private_secret = native.paths.user / "auth.json"
    private_secret.parent.mkdir(parents=True, exist_ok=True)
    private_secret.write_text('{"token": "secret"}')

    # Default RecordingUI denies prompts ('n'): .git, AGENTS.md, CLAUDE.md succeed without prompting;
    # .virgo/settings.json prompts and is denied; private auth.json is hard-denied without prompting.
    faux.script(
        "test",
        faux.reply(tools=[
            ("write", {"path": "AGENTS.md", "content": "# Repo guide\n"}),
            ("write", {"path": "CLAUDE.md", "content": "# Claude guide\n"}),
            ("write", {"path": ".git/config", "content": "[core]\n"}),
            ("write", {"path": ".virgo/settings.json", "content": "{}"}),
            ("read", {"path": str(private_secret)}),
            ("request_permissions", {
                "permissions": {"read_paths": [str(private_secret)]},
                "reason": "Attempt to escalate private Virgo credentials",
            }),
        ]),
        faux.reply("done"),
    )
    native.prompt("Update docs and git metadata, test .virgo confirmation, and verify credential hard-deny")
    agents_res, claude_res, git_res, virgo_res, priv_read, priv_req = tool_results(native)[-6:]
    assert not agents_res.is_error and (project / "AGENTS.md").read_text() == "# Repo guide\n"
    assert not claude_res.is_error and (project / "CLAUDE.md").read_text() == "# Claude guide\n"
    assert not git_res.is_error and (project / ".git/config").read_text() == "[core]\n"
    assert virgo_res.is_error and virgo_res.details["status"] == "resource_denied"
    assert virgo_res.details["boundary"]["dimension"] == "write_paths"
    assert not (project / ".virgo/settings.json").exists()
    assert priv_read.is_error and priv_read.details["boundary"]["dimension"] == "protected_path"
    assert priv_read.details["boundary"]["escalation_available"] is False
    assert priv_req.is_error and "Protected resources cannot be granted" in priv_req.text()
    # Only .virgo/settings.json triggered a confirmation prompt!
    assert len(native.ui.questions) == 1 and ".virgo" in native.ui.questions[0]

    # On macOS Seatbelt, unapproved Shell creation of .virgo/settings.json is also blocked.
    if sys.platform == "darwin":
        script = (
            "from pathlib import Path\n"
            "p = Path('.virgo/settings.json')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text('tampered')\n"
        )
        result = run(native, python_command(native, script))
        assert result.is_error, f"macOS Seatbelt must reject creating .virgo/settings.json: {result.text()}"
        assert not (project / ".virgo/settings.json").exists()

    # Approving 'y' on a subsequent turn allows writing .virgo/settings.json cleanly.
    native.ui.answers.append("y")
    faux.script(
        "test",
        faux.reply(tools=[("write", {"path": ".virgo/settings.json", "content": '{"theme": "dark"}\n'})]),
        faux.reply("done"),
    )
    native.prompt("Approve updating .virgo/settings.json")
    approved_virgo = tool_results(native)[-1]
    assert not approved_virgo.is_error, approved_virgo.text()
    assert (project / ".virgo/settings.json").read_text() == '{"theme": "dark"}\n'


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS /tmp -> /private/tmp symlink resolution")
def test_ver04_macos_tmp_symlink_grant_and_real_path_dialog(native, project):
    """VER-04 (part 1): Approving /tmp/<dir> resolves to /private/tmp/<dir> and works for Read and Shell."""
    with tempfile.TemporaryDirectory(prefix="virgo-ver04-", dir="/tmp") as raw_dir:
        raw_path = Path("/tmp") / Path(raw_dir).name
        real_path = raw_path.resolve()
        assert str(raw_path) != str(real_path)
        sample = real_path / "data.txt"
        sample.write_text("from-tmp")

        shell = selected(native).tool_name
        cmd = {"command": python_command(native, f"from pathlib import Path; print(Path({str(raw_path / 'data.txt')!r}).read_text())")}
        native.ui.answers.extend(["y", "y"])
        faux.script(
            "test",
            faux.reply(tools=[("read", {"path": str(raw_path / "data.txt")})]),
            faux.reply(tools=[("request_permissions", {
                "tool": shell, "arguments": cmd,
                "permissions": {"read_paths": [str(raw_path)]},
                "reason": "Read temporary fixture via /tmp path",
            })]),
            faux.reply(tools=[(shell, cmd)]),
            faux.reply("done"),
        )
        native.prompt("Read via /tmp symlink and run approved shell command")
        read_res, req_res, shell_res = tool_results(native)[-3:]
        assert not read_res.is_error and "from-tmp" in read_res.text(), read_res.text()
        assert not req_res.is_error, req_res.text()
        assert not shell_res.is_error and "from-tmp" in shell_res.text(), shell_res.text()
        # Both approval dialogs must display the resolved real path (/private/tmp/...).
        assert len(native.ui.questions) == 2
        assert all(str(real_path) in q for q in native.ui.questions)


@pytest.mark.skipif(os.name != "posix", reason="POSIX metadata symlink")
def test_ver04_claude_md_symlink_to_agents_md_and_single_file_edit(broker_session, project, tmp_path):
    """VER-04 (parts 2 & 3): CLAUDE.md -> AGENTS.md works seamlessly; single external file Edit reuses session grant."""
    agents = project / "AGENTS.md"
    agents.write_text("project instructions")
    claude = project / "CLAUDE.md"
    claude.symlink_to(agents)

    external_rc = tmp_path / ".zshrc"
    external_rc.write_text("export FOO=1\n")

    session = broker_session(ui=RecordingUI(["y", "n"]))
    faux.script(
        "test",
        faux.reply(tools=[("read", {"path": "CLAUDE.md"})]),
        faux.reply(tools=[
            ("write", {"path": "AGENTS.md", "content": "updated instructions"}),
            ("edit", {"path": str(external_rc), "edits": [{"old_text": "FOO=1", "new_text": "FOO=2"}]}),
        ]),
        faux.reply(tools=[
            ("edit", {"path": str(external_rc), "edits": [{"old_text": "FOO=2", "new_text": "FOO=3"}]}),
            ("write", {"path": ".virgo/settings.json", "content": "{}"}),
        ]),
        faux.reply("done"),
    )
    session.prompt("Read CLAUDE.md, update AGENTS.md, edit approved external file twice, and decline .virgo write")
    read_res, agents_write, edit1_res, edit2_res, virgo_write = tool_results(session)
    assert not read_res.is_error and "project instructions" in read_res.text()
    assert not agents_write.is_error and agents.read_text() == "updated instructions"
    assert not edit1_res.is_error, edit1_res.text()
    assert not edit2_res.is_error, edit2_res.text()
    assert external_rc.read_text() == "export FOO=3\n"
    assert virgo_write.is_error and virgo_write.details["status"] == "resource_denied"
    # Approval prompt #0 shows only the resolved external file (reused for edit2); prompt #1 is .virgo.
    assert len(session.ui.questions) == 2
    assert str(external_rc.resolve()) in session.ui.questions[0]
    assert ".virgo" in session.ui.questions[1]


def test_ver05_session_shell_write_grant_persists_in_session_until_revoked(native, project, tmp_path):
    """VER-05: Session Shell grant ('y') allows multiple commands in the same session until revoked."""
    outside = tmp_path / "external-out"
    outside.mkdir()
    shell = selected(native).tool_name
    op1 = {"command": python_command(
        native,
        f"from pathlib import Path; Path({str(outside / 'stamp.txt')!r}).write_text('first')",
    )}
    op2 = {"command": python_command(
        native,
        f"from pathlib import Path; Path({str(outside / 'stamp.txt')!r}).write_text('second')",
    )}
    native.ui.answers.append("y")
    # request_permissions works without requiring tool/arguments and can be batched in the same turn!
    faux.script(
        "test",
        faux.reply(tools=[
            ("request_permissions", {
                "permissions": {"write_paths": [str(outside)]},
                "reason": "Write external output files in this session",
            }),
            (shell, op1),
        ]),
        faux.reply(tools=[(shell, op2)]),
        faux.reply("done"),
    )
    native.prompt("Grant session write access and run two shell writes")
    req_res, first_run, second_run = tool_results(native)[-3:]
    assert not req_res.is_error, req_res.text()
    assert not first_run.is_error, first_run.text()
    assert not second_run.is_error, second_run.text()
    assert (outside / "stamp.txt").read_text() == "second"
    assert len(native.ui.questions) == 1

    # Revoking clears session grants; a subsequent Shell write to outside fails.
    native.services["execution"].revoke()
    third_run = run(native, op1["command"])
    assert third_run.is_error
    assert (outside / "stamp.txt").read_text() == "second"


def test_ver07_legacy_grants_db_purged_on_startup(make_session, project):
    """S3-06 / VER-07: Legacy resource-grants.sqlite3 is removed on startup without migration."""
    user_dir = project.parent / "home"
    user_dir.mkdir(parents=True, exist_ok=True)
    legacy = user_dir / LEGACY_GRANTS_FILENAME
    legacy.write_bytes(b"legacy sqlite data")
    (user_dir / f"{LEGACY_GRANTS_FILENAME}-wal").write_bytes(b"wal")

    ui = RecordingUI()
    session = make_session(ui=ui, extensions=False, refresh_models=False)
    try:
        assert not legacy.exists()
        assert not (user_dir / f"{LEGACY_GRANTS_FILENAME}-wal").exists()
        assert any("resource-grants.sqlite3" in line for line in ui.lines)
    finally:
        session.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="POSIX $HOME workspace")
def test_s305_posix_home_as_workspace_carves_out_private_dir(make_session, tmp_path, monkeypatch):
    """S3-05: POSIX workspace containing private ~/.virgo runs cleanly while hiding private state."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    private_dir = fake_home / ".virgo"
    private_dir.mkdir(mode=0o700)
    (private_dir / "auth.json").write_text('{"secret": "top-secret"}')
    (fake_home / "notes.txt").write_text("hello home")

    monkeypatch.setenv("VIRGO_HOME", str(private_dir))
    session = make_session(
        cwd=fake_home, home=private_dir,
        settings={"execution": {"mode": "workspace-edit"}},
        extensions=False, refresh_models=False,
    )
    try:
        faux.script(
            "test",
            faux.reply(tools=[
                ("read", {"path": "notes.txt"}),
                ("read", {"path": ".virgo/auth.json"}),
                ("write", {"path": "created.txt", "content": "ok"}),
                ("glob", {"pattern": "**/*"}),
            ]),
            faux.reply("done"),
        )
        session.prompt("Work inside $HOME while private .virgo stays carved out")
        read_ok, read_priv, write_ok, glob_ok = tool_results(session)
        assert not read_ok.is_error and "hello home" in read_ok.text()
        assert read_priv.is_error and read_priv.details["status"] == "resource_denied"
        assert not write_ok.is_error and (fake_home / "created.txt").read_text() == "ok"
        assert not glob_ok.is_error and "auth.json" not in glob_ok.text()
    finally:
        session.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="Linux bwrap argument generation requires POSIX paths")
def test_s308_linux_bwrap_wide_ro_and_explicit_tmp_grant(tmp_path):
    """S3-08: Linux bwrap mounts / read-only by default and binds /tmp rw only when explicitly granted."""
    ws = tmp_path / "ws"
    ws.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    ro_tmp_policy = NativePolicy(ws, False, False, (), (), read_only_paths=(ws / ".virgo",))
    args_ro = ro_tmp_policy._linux(Path("/usr/bin/bwrap"), scratch, ws, 3)
    mounts_ro = [args_ro[i : i + 3] for i, a in enumerate(args_ro) if a in {"--bind", "--ro-bind"}]
    assert ["--ro-bind", "/", "/"] in mounts_ro
    assert ["--bind", "/tmp", "/tmp"] not in mounts_ro

    rw_tmp_policy = NativePolicy(ws, False, False, (), ((Path("/tmp"), True),))
    args_rw = rw_tmp_policy._linux(Path("/usr/bin/bwrap"), scratch, ws, 3)
    mounts_rw = [args_rw[i : i + 3] for i, a in enumerate(args_rw) if a in {"--bind", "--ro-bind"}]
    assert ["--bind", "/tmp", "/tmp"] in mounts_rw

    # S3-02: Symlinked .virgo protects non-private target without exposing private roots.
    private_dir = tmp_path / "priv"
    private_dir.mkdir()
    private_file = private_dir / "secret.json"
    private_file.write_text("{}")
    inside_meta = ws / "virgo-meta"
    inside_meta.mkdir()
    (ws / ".virgo").symlink_to(inside_meta, target_is_directory=True)
    pol_with_sym = NativePolicy(ws, False, False, (private_dir.resolve(),), (), read_only_paths=(ws / ".virgo",))
    assert inside_meta.resolve() in pol_with_sym.protected_paths()
    (ws / ".virgo").unlink()
    (ws / ".virgo").symlink_to(private_file)
    assert private_file.resolve() not in pol_with_sym.protected_paths()


def test_s310_invalid_utf8_prompts_and_system_md_warn_without_crashing(make_session, project):
    """S3-10: Invalid UTF-8 in .virgo/prompts/*.md or SYSTEM.md warns and skips cleanly."""
    prompts = project / ".virgo" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "broken.md").write_bytes(b"\xff\xfe invalid utf8")
    (prompts / "good.md").write_text("Valid prompt body", encoding="utf-8")
    (project / ".virgo" / "SYSTEM.md").write_bytes(b"\x80\x81 bad system")

    ui = RecordingUI()
    session = make_session(ui=ui, extensions=False, refresh_models=False)
    try:
        assert "good" in session.templates
        assert "broken" not in session.templates
        assert any("invalid UTF-8" in line for line in ui.lines)
    finally:
        session.shutdown()


def test_s311_glob_match_no_exponential_backtracking_and_walk_limit(tmp_path):
    """S3-11: Consecutive ** segments finish instantly and walk_files enforces max_files."""
    from virgo.execution_policy import HOST_MODE
    from virgo.file_access import walk_files

    deep_path = "/".join(["a"] * 25) + "/x.txt"
    evil_pattern = "/".join(["**"] * 20) + "/y.txt"
    started = time.monotonic()
    assert glob_match(deep_path, evil_pattern) is False
    assert time.monotonic() - started < 0.2
    assert glob_match("a/b/c/test.py", "**/**/**/*.py") is True

    for i in range(15):
        (tmp_path / f"f{i}.txt").write_text("x")
    found = list(walk_files(tmp_path, HOST_MODE.policy, set(), max_files=5))
    assert len(found) == 5


def test_ver13_native_windows_defaults_to_workspace_edit_and_prechecks_appcontainer(project, monkeypatch):
    """VER-13: Supported Win11 (Feature 60692039) defaults to Workspace-Edit with no experimental badge."""
    import types

    import virgo._windows_probe as wp
    import virgo.execution_policy as ep
    import virgo.harness.session as hs
    import virgo.sandbox as sb
    from virgo.harness.session import AgentSession

    precheck_calls = 0

    def ok_precheck():
        nonlocal precheck_calls
        precheck_calls += 1

    fake_ws = types.SimpleNamespace(
        is_windows_sandbox_supported=lambda: True,
        precheck_windows_sandbox=ok_precheck,
    )
    monkeypatch.setattr(wp, "is_windows_appcontainer_supported", lambda: True)
    monkeypatch.setitem(sys.modules, "virgo.sandbox.windows_sandbox", fake_ws)
    monkeypatch.setattr(sb, "windows_sandbox", fake_ws, raising=False)
    monkeypatch.setattr(ep, "is_native_windows", lambda: True)
    monkeypatch.setattr(hs, "is_native_windows", lambda: True)
    assert ep.is_windows_sandbox_supported() is True
    assert ep.default_execution_mode() == "workspace-edit"

    ui = RecordingUI()
    session = AgentSession(
        project, ui=ui, model="faux/test", session=None, extensions=False, refresh_models=False,
    )
    try:
        assert session.services["execution"].selected_mode_id == "workspace-edit"
        assert precheck_calls >= 1
        # No startup No-Limit / WSL2 / experimental warning on supported Win11
        assert not any("experimental" in line.lower() or "no-limit" in line.lower() for line in ui.lines)

        session.prompt("/permissions list")
        assert "experimental" not in ui.lines[-1].lower()
        session.prompt("/permissions show")
        assert "[experimental]" not in ui.lines[-1].lower()

        # Precheck fails closed cleanly when Windows filtering services (BFE/MpsSvc) are unavailable
        def fail_network_services():
            raise ExecutionUnavailable(
                "Windows network isolation requires the running BFE service.",
                reason="windows_network_isolation_unavailable",
                dimension="windows_appcontainer_sandbox",
            )

        fake_ws.precheck_windows_sandbox = fail_network_services
        with pytest.raises(ExecutionUnavailable) as exc_info:
            session.services["execution"].select_mode("read-only")
        assert exc_info.value.reason == "windows_network_isolation_unavailable"
        assert session.services["execution"].selected_mode_id == "workspace-edit"
    finally:
        session.shutdown()


def test_ver13_unsupported_native_windows_defaults_to_no_limit_with_guidance(project, monkeypatch):
    """Unsupported Windows (without Feature 60692039 / \\Device\\Null AAP) defaults to No-Limit with 3 options."""
    import struct
    import types

    import virgo._windows_probe as wp
    import virgo.execution_policy as ep
    import virgo.harness.session as hs
    import virgo.sandbox as sb
    from virgo.harness.session import AgentSession

    # Pure-Python DACL check: stock Server 2025 (WD-only) fails, Win11 null.sys 0x1201BF (AC) passes,
    # explicit DENY on AC fails, and NULL DACL passes.
    def _ace(ace_type: int, mask: int, sid: bytes) -> bytes:
        return struct.pack("<BBHI", ace_type, 0, 8 + len(sid), mask) + sid

    wd_only = [_ace(0, 0x001201BF, wp.WORLD_SID_BYTES)]
    win11_null = [*wd_only, _ace(0, 0x001201BF, wp.AAP_SID_BYTES)]
    denied_aap = [_ace(1, 0x00000002, wp.AAP_SID_BYTES), *win11_null]
    assert wp.dacl_allows_aap_null_access(wd_only) is False
    assert wp.dacl_allows_aap_null_access(win11_null) is True
    assert wp.dacl_allows_aap_null_access(denied_aap) is False
    assert wp.dacl_allows_aap_null_access([], is_null=True) is True

    def fail_null_device():
        raise ExecutionUnavailable(
            "Native Windows AppContainer sandboxing requires Windows 11 with updated null.sys "
            "(Feature 60692039). Options:\n"
            "  1. WSL2\n"
            "  2. No-Limit (--permission no-limit)\n"
            "  3. Update Windows",
            reason="windows_null_device_unavailable",
            dimension="windows_null_device",
        )

    fake_ws = types.SimpleNamespace(
        is_windows_sandbox_supported=lambda: False,
        precheck_windows_sandbox=fail_null_device,
    )
    monkeypatch.setattr(wp, "is_windows_appcontainer_supported", lambda: False)
    monkeypatch.setitem(sys.modules, "virgo.sandbox.windows_sandbox", fake_ws)
    monkeypatch.setattr(sb, "windows_sandbox", fake_ws, raising=False)
    monkeypatch.setattr(ep, "is_native_windows", lambda: True)
    monkeypatch.setattr(hs, "is_native_windows", lambda: True)

    assert ep.is_windows_sandbox_supported() is False
    assert ep.default_execution_mode() == "no-limit"

    ui = RecordingUI()
    session = AgentSession(
        project, ui=ui, model="faux/test", session=None, extensions=False, refresh_models=False,
    )
    try:
        assert session.services["execution"].selected_mode_id == "no-limit"
        notice = "\n".join(ui.lines)
        assert "WSL2" in notice
        assert "No-Limit" in notice
        assert "Update Windows" in notice
        assert "60692039" in notice

        # Second startup in the same VIRGO_HOME must not repeat the one-time notice.
        ui2 = RecordingUI()
        second = AgentSession(
            project, ui=ui2, model="faux/test", session=None, extensions=False, refresh_models=False,
        )
        try:
            assert not any("WSL2" in line for line in ui2.lines)
        finally:
            second.shutdown()

        # Explicit sandbox selection fails closed with the 3 options rather than falling back silently.
        with pytest.raises(ExecutionUnavailable) as exc_info:
            session.services["execution"].select_mode("workspace-edit")
        assert exc_info.value.reason == "windows_null_device_unavailable"
        msg = str(exc_info.value)
        assert "WSL2" in msg and "No-Limit" in msg and "Update Windows" in msg
        assert session.services["execution"].selected_mode_id == "no-limit"
    finally:
        session.shutdown()
