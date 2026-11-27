"""Broker policy boundary tests: symlinks, $HOME carveouts, and platform default mode selection."""
from __future__ import annotations

import os
import sys

import pytest

from virgo.ai.providers import faux
from virgo.execution import ExecutionUnavailable

from tests.support.harness import RecordingUI, tool_results


@pytest.mark.skipif(os.name != "posix", reason="POSIX metadata symlink")
def test_claude_md_symlink_to_agents_md_and_single_file_edit(broker_session, project, tmp_path):
    """CLAUDE.md -> AGENTS.md works seamlessly; single external file Edit reuses session grant."""
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX $HOME workspace")
def test_posix_home_as_workspace_carves_out_private_dir(make_session, tmp_path, monkeypatch):
    """POSIX workspace containing private ~/.virgo runs cleanly while hiding private state."""
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


def test_native_windows_defaults_to_workspace_edit_and_prechecks_appcontainer(project, monkeypatch):
    """Supported Win11 (Feature 60692039) defaults to Workspace-Edit with no experimental badge."""
    import types

    import virgo.execution_policy as ep
    import virgo.harness.session as hs
    import virgo.sandbox as sb
    import virgo.sandbox.windows_acl as wa
    from virgo.harness.session import AgentSession

    precheck_calls = 0

    def ok_precheck():
        nonlocal precheck_calls
        precheck_calls += 1

    fake_ws = types.SimpleNamespace(
        precheck_windows_sandbox=ok_precheck,
    )
    monkeypatch.setattr(wa, "is_windows_appcontainer_supported", lambda: True)
    monkeypatch.setitem(sys.modules, "virgo.sandbox.windows_sandbox", fake_ws)
    monkeypatch.setattr(sb, "windows_sandbox", fake_ws, raising=False)
    monkeypatch.setattr(ep, "is_native_windows", lambda: True)
    monkeypatch.setattr(hs, "is_native_windows", lambda: True)
    import virgo.execution as ex

    assert ex.is_windows_sandbox_supported() is True
    assert ex.default_execution_mode() == "workspace-edit"

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


def test_unsupported_native_windows_defaults_to_no_limit_with_guidance(project, monkeypatch):
    """Unsupported Windows (without Feature 60692039 / \\Device\\Null AAP) defaults to No-Limit with 3 options."""
    import struct
    import types

    import virgo.execution_policy as ep
    import virgo.harness.session as hs
    import virgo.sandbox as sb
    import virgo.sandbox.windows_acl as wa
    from virgo.harness.session import AgentSession

    # Pure-Python DACL check: stock Server 2025 (WD-only) fails, Win11 null.sys 0x1201BF (AC) passes,
    # explicit DENY on AC fails, and NULL DACL passes.
    def _ace(ace_type: int, mask: int, sid: bytes) -> bytes:
        return struct.pack("<BBHI", ace_type, 0, 8 + len(sid), mask) + sid

    wd_only = [_ace(0, 0x001201BF, wa.WORLD_SID_BYTES)]
    win11_null = [*wd_only, _ace(0, 0x001201BF, wa.AAP_SID_BYTES)]
    denied_aap = [_ace(1, 0x00000002, wa.AAP_SID_BYTES), *win11_null]
    assert wa.dacl_allows_aap_null_access(wd_only) is False
    assert wa.dacl_allows_aap_null_access(win11_null) is True
    assert wa.dacl_allows_aap_null_access(denied_aap) is False
    assert wa.dacl_allows_aap_null_access([], is_null=True) is True

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
        precheck_windows_sandbox=fail_null_device,
    )
    monkeypatch.setattr(wa, "is_windows_appcontainer_supported", lambda: False)
    monkeypatch.setitem(sys.modules, "virgo.sandbox.windows_sandbox", fake_ws)
    monkeypatch.setattr(sb, "windows_sandbox", fake_ws, raising=False)
    monkeypatch.setattr(ep, "is_native_windows", lambda: True)
    monkeypatch.setattr(hs, "is_native_windows", lambda: True)

    import virgo.execution as ex

    assert ex.is_windows_sandbox_supported() is False
    assert ex.default_execution_mode() == "no-limit"

    # Explicit No-Limit does not probe Windows AppContainer support or emit the default-downgrade notice.
    explicit_ui = RecordingUI()
    probe_calls = 0

    def fail_probe():
        nonlocal probe_calls
        probe_calls += 1
        return False

    monkeypatch.setattr(wa, "is_windows_appcontainer_supported", fail_probe)
    explicit = AgentSession(
        project, ui=explicit_ui, model="faux/test", session=None,
        settings={"execution": {"mode": "no-limit"}}, extensions=False, refresh_models=False,
    )
    try:
        assert explicit.services["execution"].selected_mode_id == "no-limit"
        assert probe_calls == 0
        assert not any("WSL2" in line for line in explicit_ui.lines)
    finally:
        explicit.shutdown()
    monkeypatch.setattr(wa, "is_windows_appcontainer_supported", lambda: False)

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
