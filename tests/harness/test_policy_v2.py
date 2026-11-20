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


def test_ver01_empty_project_protects_nonexistent_control_files(native, project):
    """VER-01: Write (all OS) and Shell (macOS) cannot create protected names in an empty project."""
    targets = [".virgo/settings.json", "AGENTS.md", "CLAUDE.md", ".git/config"]
    faux.script(
        "test",
        faux.reply(tools=[("write", {"path": path, "content": "tampered"}) for path in targets]),
        faux.reply("done"),
    )
    native.prompt("Attempt to create protected control files in an empty workspace")
    results = tool_results(native)[-len(targets):]
    assert all(result.is_error and result.details["status"] == "resource_denied" for result in results)
    for path in targets:
        assert not (project / path).exists()

    if sys.platform == "darwin":
        for path in targets:
            script = (
                f"from pathlib import Path\n"
                f"p = Path({path!r})\n"
                f"p.parent.mkdir(parents=True, exist_ok=True)\n"
                f"p.write_text('tampered')\n"
            )
            result = run(native, python_command(native, script))
            assert result.is_error, f"macOS Seatbelt must reject creating {path}: {result.text()}"
            assert not (project / path).exists()


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
    """VER-04 (parts 2 & 3): CLAUDE.md -> AGENTS.md starts cleanly and protects both; single-file Edit works."""
    agents = project / "AGENTS.md"
    agents.write_text("project instructions")
    claude = project / "CLAUDE.md"
    claude.symlink_to(agents)

    external_rc = tmp_path / ".zshrc"
    external_rc.write_text("export FOO=1\n")

    session = broker_session(ui=RecordingUI(["y"]))
    faux.script(
        "test",
        faux.reply(tools=[
            ("read", {"path": "CLAUDE.md"}),
            ("write", {"path": "CLAUDE.md", "content": "bad"}),
            ("write", {"path": "AGENTS.md", "content": "bad"}),
            ("edit", {"path": str(external_rc), "edits": [{"old_text": "FOO=1", "new_text": "FOO=2"}]}),
        ]),
        faux.reply("done"),
    )
    session.prompt("Read CLAUDE.md, attempt protected writes, and edit an approved single external file")
    read_res, claude_write, agents_write, edit_res = tool_results(session)
    assert not read_res.is_error and "project instructions" in read_res.text()
    assert claude_write.is_error and claude_write.details["status"] == "resource_denied"
    assert agents_write.is_error and agents_write.details["status"] == "resource_denied"
    assert agents.read_text() == "project instructions"
    assert not edit_res.is_error, edit_res.text()
    assert external_rc.read_text() == "export FOO=2\n"
    # Approval prompt must show only the single resolved file, not its parent directory.
    assert len(session.ui.questions) == 1
    assert str(external_rc.resolve()) in session.ui.questions[0]


def test_ver05_oneshot_shell_write_grant_applies_once(native, project, tmp_path):
    """VER-05: One-shot Shell grant writes once to an external dir; second identical call is denied."""
    outside = tmp_path / "external-out"
    outside.mkdir()
    shell = selected(native).tool_name
    operation = {"command": python_command(
        native,
        f"from pathlib import Path; Path({str(outside / 'stamp.txt')!r}).write_text('written')",
    )}
    native.ui.answers.append("y")
    faux.script(
        "test",
        faux.reply(tools=[("request_permissions", {
            "tool": shell, "arguments": operation,
            "permissions": {"write_paths": [str(outside)]},
            "reason": "Write one external output file",
        })]),
        faux.reply(tools=[(shell, operation)]),
        faux.reply(tools=[(shell, operation)]),
        faux.reply("done"),
    )
    native.prompt("Write once with one-shot grant, then repeat without a new grant")
    req_res, first_run, second_run = tool_results(native)[-3:]
    assert not req_res.is_error, req_res.text()
    assert not first_run.is_error, first_run.text()
    assert (outside / "stamp.txt").read_text() == "written"
    assert second_run.is_error, "Second execution must not reuse a consumed one-shot grant"


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
def test_s308_linux_bwrap_only_remounts_tmp_ro_when_not_writable(tmp_path):
    """S3-08: Linux bwrap must not lock /tmp read-only when /tmp is granted writable."""
    ws = tmp_path / "ws"
    ws.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    ro_tmp_policy = NativePolicy(ws, False, False, (), ())
    args_ro = ro_tmp_policy._linux(Path("/usr/bin/bwrap"), scratch, ws, 3, ())
    assert "--remount-ro" in args_ro and "/tmp" in args_ro
    idx = [i for i, a in enumerate(args_ro) if a == "--remount-ro"]
    assert any(args_ro[i + 1] == "/tmp" for i in idx)

    rw_tmp_policy = NativePolicy(ws, False, False, (), ((Path("/tmp"), True),))
    args_rw = rw_tmp_policy._linux(Path("/usr/bin/bwrap"), scratch, ws, 3, ())
    idx_rw = [i for i, a in enumerate(args_rw) if a == "--remount-ro"]
    assert not any(args_rw[i + 1] == "/tmp" for i in idx_rw)


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


def test_ver13_native_windows_defaults_to_no_limit_and_prechecks_experimental_sandbox(make_session, project, monkeypatch):
    """S1-04 / S7-06 / VER-13: Native Windows defaults to No-Limit with WSL2 hint; sandbox prechecks cleanly."""
    import virgo.execution_policy as ep
    import virgo.harness.execution as he
    import virgo.harness.session as hs
    import virgo.sandbox as sb

    monkeypatch.setattr(ep, "is_native_windows", lambda: True)
    monkeypatch.setattr(he, "is_native_windows", lambda: True)
    monkeypatch.setattr(hs, "is_native_windows", lambda: True)
    monkeypatch.setattr(sb, "is_native_windows", lambda: True)

    ui = RecordingUI()
    session = make_session(ui=ui, extensions=False, refresh_models=False)
    try:
        assert session.services["execution"].selected_mode_id == "no-limit"
        assert any("WSL2" in line for line in ui.lines)

        session.prompt("/permissions list")
        assert "experimental" in ui.lines[-1].lower()

        def fail_non_admin():
            raise ExecutionUnavailable(
                "The experimental native Windows sandbox requires an Administrator account. Use WSL2 or No-Limit.",
                reason="windows_admin_required",
            )

        import types
        fake_ws = types.SimpleNamespace(precheck_experimental_sandbox=fail_non_admin)
        monkeypatch.setitem(sys.modules, "virgo.sandbox.windows_sandbox", fake_ws)
        with pytest.raises(ExecutionUnavailable, match="WSL2"):
            session.services["execution"].select_mode("workspace-edit")
        assert session.services["execution"].selected_mode_id == "no-limit"
    finally:
        session.shutdown()
