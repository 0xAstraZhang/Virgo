"""Managed shell execution: exit codes, timeouts, truncation, environment, and failed downloads."""

from __future__ import annotations

import os
import shlex
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from virgo.agent.types import ToolContext
from virgo.execution import HostContext
from virgo.tools.output import MAX_LINES
from virgo.tools.shell import default_shell, discover_shells, make_shell_tool, probe_shell

from tests.support.http import serve


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory permissions")
def test_discovery_continues_past_inaccessible_path_entry(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o000)
    try:
        if os.access(blocked, os.X_OK):
            pytest.skip("current user can traverse mode-000 directories")
        candidate = tmp_path / "bash"
        candidate.touch(mode=0o755)
        monkeypatch.setenv("PATH", os.pathsep.join((str(blocked), str(tmp_path))))

        shells = discover_shells(execute=False)
        assert str(candidate) in {shell.executable for shell in shells}
    finally:
        blocked.chmod(0o700)


@pytest.fixture(params=["bash", "zsh"])
def shell(request):
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} is not installed")
    return probe_shell(executable)


@pytest.fixture
def download():
    curl = shutil.which("curl")
    if not curl:
        pytest.skip("curl is not installed")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/timeout":
                time.sleep(0.5)
                return
            self.send_response(503 if self.path == "/error" else 200)
            body = b"<feed>fresh</feed>"
            self.send_header("Content-Length", str(len(body) + (10 if self.path == "/partial" else 0)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with serve(Handler) as url:
        # Ignore personal curl configuration and proxy settings; all traffic stays local.
        yield f"{shlex.quote(curl)} -q --noproxy '*' -fsSL --max-time 0.2 {url}"


def ctx(cwd: Path) -> ToolContext:
    return ToolContext("c1", cwd, threading.Event())


def run(project: Path, command: str, *, interpreter=None, timeout: float | None = None):
    chosen = interpreter or default_shell(discover_shells())
    tool = make_shell_tool(chosen, execution=lambda c: HostContext(c.cwd))
    args = {"command": command}
    if timeout is not None:
        args["timeout"] = timeout
    return tool.execute(args, ctx(project))


@pytest.mark.parametrize(("endpoint", "code"), [("timeout", 28), ("error", 22)])
def test_failed_download_does_not_read_existing_file(project, shell, download, endpoint, code):
    old = project / "kv.xml"
    old.write_text("stale-search-result")
    result = run(project, f"{download}/{endpoint} -o kv.xml; cat kv.xml; touch consumed", interpreter=shell)
    assert result.is_error
    assert result.details["exit_code"] == code
    assert "stale-search-result" not in result.as_text()
    assert not (project / "consumed").exists()
    assert old.read_text() == "stale-search-result"


def test_failed_download_in_pipeline_is_not_reported_as_success(project, shell, download):
    result = run(project, f"{download}/error | cat; touch consumed", interpreter=shell)
    assert result.is_error and result.details["exit_code"] == 22
    assert not (project / "consumed").exists()


@pytest.mark.parametrize(("endpoint", "code"), [("partial", 18), ("ok", 0)])
def test_download_publishes_only_after_success(project, shell, download, endpoint, code):
    old = project / "kv.xml"
    old.write_text("stale-search-result")
    result = run(
        project,
        'tmp=$(mktemp ./kv.xml.XXXXXX)\n'
        'trap \'rm -f "$tmp"\' EXIT\n'
        f'{download}/{endpoint} -o "$tmp"\n'
        'test -s "$tmp"\n'
        'mv "$tmp" kv.xml\n'
        'cat kv.xml',
        interpreter=shell,
    )
    assert result.details["exit_code"] == code
    assert result.is_error == bool(code)
    assert old.read_text() == ("stale-search-result" if code else "<feed>fresh</feed>")
    assert "stale-search-result" not in result.as_text()
    assert not list(project.glob("kv.xml.*"))
    if not code:
        assert result.as_text() == "<feed>fresh</feed>"


def test_explicit_failure_handling_still_works(project, shell, download):
    result = run(
        project,
        f"if {download}/error -o kv.xml; then cat kv.xml; else printf 'download failed\\n'; fi; "
        "printf 'handled\\n'",
        interpreter=shell,
    )
    assert not result.is_error
    assert "download failed\nhandled" in result.as_text()


def test_background_download_failure_does_not_read_old_file(make_session, project, shell, download):
    (project / "kv.xml").write_text("stale-search-result")
    session = make_session(settings={"shell": {"executable": shell.executable}})
    jobs = session.services["jobs"]
    job = jobs.start_command(f"{download}/timeout -o kv.xml; cat kv.xml; touch consumed", project)
    assert session.ui.woken.wait(5)
    assert job.status == "failed" and job.exit_code == 28
    assert "stale-search-result" not in jobs.output(job)
    assert not (project / "consumed").exists()


def test_bash_exit_codes_timeout_and_truncation(project, python_command):
    interpreter = default_shell(discover_shells())
    exit_cmd = "Write-Output hi; exit 3" if interpreter.tool_name == "powershell" else "echo hi; exit 3"
    r = run(project, exit_cmd, interpreter=interpreter)
    assert r.is_error and "hi" in r.as_text() and "Exit code 3" in r.as_text()
    cwd_out = run(project, python_command("import os; print(os.getcwd())", shell=interpreter), interpreter=interpreter)
    assert not cwd_out.is_error and Path(cwd_out.as_text().strip()).resolve() == project.resolve()
    t = run(project, python_command("import time; time.sleep(5)", shell=interpreter), interpreter=interpreter, timeout=0.3)
    assert t.is_error and "timed out" in t.as_text()
    big_res = run(project, python_command(f"for i in range(1, {MAX_LINES + 101}): print(i)", shell=interpreter), interpreter=interpreter)
    big = big_res.as_text()
    assert "Output truncated" in big and big.rstrip().endswith(str(MAX_LINES + 100))
    output_id = big_res.details["output_id"]
    assert output_id and output_id.startswith("out_") and output_id in big
    assert not (project / ".virgo" / "outputs").exists()


def test_bash_marks_child_processes(project, python_command):
    interpreter = default_shell(discover_shells())
    result = run(project, python_command("import os; print(os.environ['VIRGO'])", shell=interpreter), interpreter=interpreter)
    assert not result.is_error and result.as_text() == "1"


def test_sandbox_failure_hint_explains_tmp_and_write_boundary_denials():
    from virgo.tools.shell import sandbox_failure_hint

    seatbelt_boundary = {
        "provider": "sandbox",
        "backend": "seatbelt",
        "effective": {
            "native_process": {
                "write_paths": ["/Users/alice/project"],
                "unrestricted": False,
            }
        },
    }
    # macOS curl writing to /tmp under Seatbelt fails with exit 56 and cryptic callback error
    curl_hint = sandbox_failure_hint(
        'curl -sS "https://example.com/a.xml" -o /tmp/kv.xml',
        "curl: (56) Failure writing output to destination, passed 1369 returned 4294967295",
        seatbelt_boundary,
    )
    assert curl_hint is not None
    assert "/tmp" in curl_hint and "$TMPDIR" in curl_hint

    # Linux bwrap or macOS Seatbelt EPERM / EROFS on /tmp
    erofs_hint = sandbox_failure_hint(
        "echo hi > /tmp/out.txt",
        "bash: /tmp/out.txt: Read-only file system",
        {**seatbelt_boundary, "backend": "bubblewrap"},
    )
    assert erofs_hint is not None
    assert "/tmp" in erofs_hint and "$TMPDIR" in erofs_hint

    # When /tmp is explicitly granted writable, do not claim /tmp is read-only
    tmp_granted = {
        "provider": "sandbox",
        "backend": "seatbelt",
        "effective": {"native_process": {"write_paths": ["/Users/alice/project", "/tmp"], "unrestricted": False}},
    }
    granted_hint = sandbox_failure_hint(
        "touch /tmp/out.txt",
        "touch: /tmp/out.txt: Permission denied",
        tmp_granted,
    )
    assert granted_hint is not None
    assert "Host /tmp is read-only" not in granted_hint
    assert "write_paths" in granted_hint

    # Read-only sandbox mode (empty write_paths)
    readonly_boundary = {
        "provider": "sandbox",
        "backend": "seatbelt",
        "effective": {"native_process": {"write_paths": [], "unrestricted": False}},
    }
    ro_hint = sandbox_failure_hint("touch a.txt", "touch: a.txt: Operation not permitted", readonly_boundary)
    assert ro_hint is not None
    assert "read-only" in ro_hint and "$TMPDIR" in ro_hint

    # Non-write errors (e.g. network failure, missing file) must not emit false-positive hints
    assert sandbox_failure_hint(
        'curl -sS "https://example.com" -o /tmp/kv.xml',
        "curl: (52) Empty reply from server",
        seatbelt_boundary,
    ) is None
    assert sandbox_failure_hint(
        "cat /tmp/missing.txt",
        "cat: /tmp/missing.txt: No such file or directory",
        seatbelt_boundary,
    ) is None

    # Host / No-Limit provider must never emit sandbox hints
    assert sandbox_failure_hint(
        "touch /tmp/x",
        "touch: /tmp/x: Permission denied",
        {"provider": "host"},
    ) is None

    # Native Windows AppContainer hints must use Windows temp variables and omit host /tmp
    win_ro_boundary = {
        "provider": "sandbox",
        "backend": "windows-appcontainer",
        "effective": {"native_process": {"write_paths": [], "unrestricted": False}},
    }
    win_ro_hint = sandbox_failure_hint(
        "Set-Content a.txt hi",
        "UnauthorizedAccessException: Access to the path is denied.",
        win_ro_boundary,
    )
    assert win_ro_hint is not None
    assert "$env:TMPDIR" in win_ro_hint and "$env:TEMP" in win_ro_hint and "%TEMP%" in win_ro_hint
    assert "/tmp" not in win_ro_hint

    win_rw_boundary = {
        "provider": "sandbox",
        "backend": "windows-appcontainer",
        "effective": {"native_process": {"write_paths": ["C:\\work\\proj"], "unrestricted": False}},
    }
    win_rw_hint = sandbox_failure_hint(
        "Set-Content C:\\outside.txt hi",
        "Access is denied.",
        win_rw_boundary,
    )
    assert win_rw_hint is not None
    assert "$env:TMPDIR" in win_rw_hint and "$env:TEMP" in win_rw_hint and "write_paths" in win_rw_hint
    assert "/tmp" not in win_rw_hint
