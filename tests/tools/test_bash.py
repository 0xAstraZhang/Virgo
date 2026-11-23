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
from virgo.i18n import Translator
from virgo.tools.output import MAX_LINES, CommandOutput
from virgo.tools.shell import (
    Shell,
    default_shell,
    discover_shells,
    make_shell_tool,
    probe_shell,
    sandbox_failure_hint,
)

from tests.support.http import serve


@pytest.mark.parametrize("kind", ["bash", "zsh", "powershell"])
@pytest.mark.parametrize(("language", "guidance"), [
    ("en", ("When available, prefer", "read, write, edit, glob, grep",
            "over shell commands or inline scripts", "reading, searching, or editing files")),
    ("zh_CN", ("优先使用可用的专用文件工具", "read、write、edit、glob、grep",
               "避免为此运行 Shell 命令或内联脚本", "读取、搜索或编辑文件")),
])
def test_shell_description_prefers_available_file_tools(kind, language, guidance):
    tool = make_shell_tool(Shell(kind, kind, ""))
    description = tool.spec(Translator(language)).description
    if language == "en":
        assert tool.description == description
    for phrase in guidance:
        assert phrase in description


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


_CASE53A_COMMAND = 'ps -p 21839 -o pid=,stat=,command=; status=$?; echo "ps status=$status"; wc -l < ledger.log'
_CASE53A_DENIAL = "/bin/bash: /bin/ps: Operation not permitted"


@pytest.fixture
def workspace_boundary():
    return {
        "provider": "sandbox",
        "backend": "seatbelt",
        "effective": {"native_process": {
            "read_paths": ["/workspace"], "write_paths": ["/workspace"],
            "network_allowed": True, "unrestricted": False, "web_allowed": True,
        }},
    }


def test_case53a_shell_result_preserves_native_denial(project, monkeypatch, workspace_boundary):
    def recorded_run(command, *args, **kwargs):
        assert command == _CASE53A_COMMAND
        return CommandOutput(_CASE53A_DENIAL, boundary=workspace_boundary), 1, "ok"

    monkeypatch.setattr("virgo.tools.bash.run_command", recorded_run)
    result = make_shell_tool(Shell("bash", "/bin/bash", "3.2")).execute({"command": _CASE53A_COMMAND}, ctx(project))

    assert result.is_error
    assert result.details["exit_code"] == 1
    assert result.details["boundary"] == workspace_boundary
    assert result.as_text() == Translator()("tools.output.exit", text=_CASE53A_DENIAL, code=1)


@pytest.mark.parametrize(("command", "diagnostic"), [
    pytest.param(_CASE53A_COMMAND, _CASE53A_DENIAL, id="case53a"),
    pytest.param("pgrep workers", "pgrep: Operation not permitted", id="pgrep"),
    pytest.param("sysctl -a", "sysctl: EPERM", id="sysctl"),
    pytest.param("cat /root/secret", "cat: /root/secret: Permission denied", id="cat"),
    pytest.param("ls /root", "ls: /root: EACCES", id="ls"),
    pytest.param("./script.sh", "bash: ./script.sh: Permission denied", id="exec"),
    pytest.param(shlex.join(["python", "-c", "open('/tmp/in').read()"]), "PermissionError: [Errno 13] Permission denied: '/tmp/in'",
                 id="python-read"),
    pytest.param("Get-Content secret", "System.UnauthorizedAccessException: Access is denied.", id="powershell-read"),
    pytest.param("echo ok > ok.txt; /bin/ps -p 1", _CASE53A_DENIAL, id="write-then-ps"),
    pytest.param("echo ok > /tmp/ok.txt; ps -p 1", _CASE53A_DENIAL, id="tmp-write-then-ps"),
    pytest.param("touch ok.txt && cat /root/secret", "cat: /root/secret: Permission denied", id="write-then-read"),
    pytest.param("touch same && cat same", "cat: same: Permission denied", id="same-target-read"),
    pytest.param("cat /root/secret > out", "cat: /root/secret: Permission denied", id="redirect-but-read-failed"),
    pytest.param("cat < /tmp/in > out", "bash: /tmp/in: Permission denied", id="input-redirect"),
    pytest.param("echo 'touch /tmp/x; > secret'; wc -l < secret", "bash: secret: Permission denied",
                 id="quoted-command-is-data"),
    pytest.param("cp /tmp/in /etc/out", "cp: /tmp/in: Permission denied", id="copy-source"),
    pytest.param("cp /etc/bar bar", "cp: /etc/bar: Permission denied", id="copy-source-same-basename"),
    pytest.param("git status", "fatal: cannot open .git/config: Permission denied", id="git-read"),
    pytest.param("tar -tf /tmp/in.tar", "tar: /tmp/in.tar: Permission denied", id="tar-list"),
    pytest.param("echo hi > same; cat < same", "bash: same: Permission denied", id="same-redirect-target"),
    pytest.param("touch config && git status", "fatal: cannot open config: Permission denied", id="git-read-after-touch"),
    pytest.param("cp /tmp/in /etc/out", "cp: cannot open '/tmp/in' for reading: Permission denied",
                 id="copy-input-diagnostic"),
    pytest.param(shlex.join(["python", "-c", "'text'.replace('in', 'new'); open('in').read()"]),
                 "PermissionError: [Errno 13] Permission denied: 'in'", id="string-replace-is-data"),
    pytest.param("touch ./script && ./script", "./script: Permission denied", id="same-target-exec"),
    pytest.param("helper", "cannot create socket: Operation not permitted", id="socket"),
    pytest.param("cp /tmp/in out", "Permission denied", id="ambiguous-copy"),
    pytest.param("Get-Content file | Set-Content other", "Get-Content : Access to the path 'file' is denied.",
                 id="powershell-pipeline-read"),
    pytest.param("touch -r /tmp/ref out", "touch: failed to get attributes of '/tmp/ref': Permission denied",
                 id="touch-reference-read"),
    pytest.param(r"Copy-Item C:\input C:\out", r"Access to the path 'C:\input' is denied.", id="powershell-copy-input"),
    pytest.param("touch same; cat same", "same: Permission denied", id="ambiguous-path-only"),
    pytest.param(shlex.join(["python", "-c", "open('same', 'w')"]) + "; " + shlex.join(["python", "-c", "open('same')"]),
                 "PermissionError: [Errno 13] Permission denied: 'same'", id="ambiguous-interpreter"),
    pytest.param("touch x; helper", "Permission denied", id="unattributed"),
])
def test_sandbox_hint_requires_write_evidence(command, diagnostic, workspace_boundary):
    assert sandbox_failure_hint(command, diagnostic, workspace_boundary) is None


@pytest.mark.parametrize(("command", "diagnostic", "hint"), [
    ("Get-Content in | Set-Content out", "UnauthorizedAccessException: Access to the path 'out' is denied.", "write"),
    ("helper", "EROFS", "write"),
    ("touch /tmp/out", "EROFS", "tmp"),
    ("sh script.sh", "mkdir: /tmp/build: Permission denied", "tmp"),
    ("unzip data.zip -d /tmp/out", "unzip: /tmp/out: Permission denied", "tmp"),
    ("touch /tmp/out", "Permission denied", "tmp"),
    ("rm '/tmp/old'", "rm: cannot remove '/tmp/old': EPERM", "tmp"),
    ("rmdir /etc/old", "rmdir: /etc/old: Operation not permitted", "write"),
    ("ln -s /tmp/in /etc/link", "ln: /etc/link: Permission denied", "write"),
    ("chmod 600 /etc/file", "chmod: /etc/file: Operation not permitted", "write"),
    ("chown me /etc/file", "chown: /etc/file: Operation not permitted", "write"),
    ("install /tmp/in /etc/out", "install: /etc/out: Permission denied", "write"),
    ("git add foo", "fatal: Unable to create '/repo/.git/index.lock': Permission denied", "write"),
    ("tar -cf /tmp/out.tar src", "tar: /tmp/out.tar: Cannot open: Permission denied", "tmp"),
    ("tar -xf archive -C /tmp/out", "tar: member: Cannot open: Permission denied", "tmp"),
    ("echo hi > /tmp/a\\ b", "bash: line 1: /tmp/a b: Permission denied", "tmp"),
    (shlex.join(["python3", "-Ic", "open('/tmp/out', 'ab')"]), "PermissionError: [Errno 1] EPERM: '/tmp/out'", "tmp"),
    ("python script.py", 'Traceback (most recent call last):\n  File "script.py", line 2, in <module>\n'
     '    open(path, "x")\nPermissionError: [Errno 13] Permission denied: \'/tmp/file\'', "tmp"),
    ("python script.py", 'Traceback (most recent call last):\n  File "script.py", line 2, in <module>\n'
     '    open(path, "x")\n    ~~~~^^^^^^^^^^^\nPermissionError: [Errno 13] Permission denied: \'/tmp/file\'', "tmp"),
    ("helper", "OSError: [Errno 30] Read-only file system: '/private/tmp/x'", "tmp"),
    ("helper", "cannot create regular file '/etc/out'", "write"),
    ("(env VAR=1 touch '/tmp/a b')", "touch: /tmp/a b: Operation not permitted", "tmp"),
    ("cat /tmp/in.txt && touch /etc/out.txt", "touch: /etc/out.txt: Permission denied", "write"),
    ("echo /tmp/input; mkdir /etc/build", "/tmp/input\nmkdir: /etc/build: EACCES", "write"),
    ("cat /tmp/in | tee /etc/out", "tee: /etc/out: Permission denied", "write"),
    ("cp /tmp/in /etc/out", "cp: /etc/out: Permission denied", "write"),
    ("cp /tmp/in /etc/out", "cp: error copying '/tmp/in' to '/etc/out': Read-only file system", "write"),
    ("cat in && curl -o /tmp/out url", "Failure writing output to destination", "tmp"),
    ("mv in '/tmp/a b'", "mv: rename in to /tmp/a b: Operation not permitted", "tmp"),
    ("cat /tmp/in\necho hi 2>> '/etc/out file'", "bash: line 2: /etc/out file: Permission denied", "write"),
    ("echo ok >| '/tmp/out file'", "zsh:1: permission denied: /tmp/out file", "tmp"),
    ("echo ok >> /tmp/out", "Permission denied", "tmp"),
    ("echo ok &>/etc/out", "bash: /etc/out: Permission denied", "write"),
    ("echo ok 1>/etc/out", "sh: 1: cannot create /etc/out: Permission denied", "write"),
    ("dd if=/tmp/in of=/etc/out", "dd: /etc/out: Permission denied", "write"),
    ("cat /tmp/in; curl --output /etc/out url", "curl: (23) Failed writing received data", "write"),
    ("curl -o /tmp/feed url; cat in", "curl: (23) Failure writing output to destination", "tmp"),
    ("/usr/bin/curl -o /tmp/feed url", "/usr/bin/curl: (56) Failure writing output to destination", "tmp"),
    (r"C:\Tools\curl.EXE -o /tmp/feed url", r"C:\Tools\curl.EXE: (56) Failure writing output to destination", "tmp"),
    ("/usr/bin/touch /tmp/out", "/usr/bin/touch: /tmp/out: Operation not permitted", "tmp"),
    ("sh script.sh", "/usr/bin/touch.exe: /tmp/out: Operation not permitted", "tmp"),
    ("echo hi > /tmp/out.txt; ps -p 1", "bash: /tmp/out.txt: Operation not permitted\n" + _CASE53A_DENIAL, "tmp"),
    ("touch /tmp/out; touch /etc/out", "touch: /tmp/out: Permission denied\ntouch: /etc/out: Permission denied", "write"),
    ("wget -O /tmp/feed url", "/tmp/feed: Permission denied", "tmp"),
    ("git init /tmp/repo", "/tmp/repo: Permission denied", "tmp"),
    ("tar -xf /tmp/in.tar -C /etc", "tar: out: Cannot open: Permission denied", "write"),
    ("unzip /tmp/in.zip -d /etc", "error: cannot create /etc/out: Permission denied", "write"),
    (shlex.join(["python", "-c", "open('/etc/out', 'w')"]), "PermissionError: [Errno 13] Permission denied: '/etc/out'", "write"),
    (shlex.join(["python", "-c", "from pathlib import Path; Path('/tmp/out').write_text('ok')"]),
     "PermissionError: [Errno 1] Operation not permitted: '/tmp/out'", "tmp"),
    (shlex.join(["python", "-c", "import os; os.makedirs('/etc/build')"]), "PermissionError: [Errno 13] EACCES: '/etc/build'", "write"),
])
def test_sandbox_hint_uses_failed_write_destination(command, diagnostic, hint, workspace_boundary):
    assert sandbox_failure_hint(command, diagnostic, workspace_boundary, t=str) == f"tools.output.sandbox_{hint}_hint"


@pytest.mark.parametrize("command", [
    r"Set-Content -Path 'C:\outside file' hi",
    r"Add-Content 'C:\outside file' hi",
    r"Out-File 'C:\outside file'",
    r"New-Item 'C:\outside file'",
    r"Copy-Item src 'C:\outside file'",
    r"Move-Item src 'C:\outside file'",
    r"Remove-Item 'C:\outside file'",
    r"Rename-Item 'C:\outside file' other",
    r"[IO.File]::WriteAllText('C:\outside file', 'ok')",
])
def test_windows_sandbox_hint_for_file_mutation(command, workspace_boundary):
    boundary = {**workspace_boundary, "backend": "windows-appcontainer"}
    assert sandbox_failure_hint(command, "Access is denied.", boundary, t=str) == "tools.output.sandbox_windows_write_hint"


@pytest.mark.parametrize(("command", "diagnostic", "write"), [
    ("Set-Content -Path 'pwsh_ro_blocked.txt' -Value 'no'",
     r"Access to the path 'C:\Users\alice\proj\pwsh_ro_blocked.txt' is denied.", True),
    ("Get-Content in | Set-Content -Path out -Value value", r"Access to the path 'C:\work\value' is denied.", False),
    ("Set-Content -Value hi out", r"Access to the path 'C:\work\out' is denied.", True),
    (r"[IO.File]::WriteAllText('C:\outside file', 'ok')", r"Access to the path 'C:\outside file' is denied.", True),
    (r"[IO.File]::WriteAllText('C:\outside,file','ok')", r"Access to the path 'C:\outside,file' is denied.", True),
    ("Get-Content in.txt | Set-Content out.txt",
     r"UnauthorizedAccessException: Access to the path 'C:\work\out.txt' is denied.", True),
    ("Get-Content in.txt | Set-Content out.txt", r"Access to the path 'C:\work\in.txt' is denied.", False),
    (r"Get-Content input\out.txt | Set-Content out.txt", r"Access to the path 'C:\work\input\out.txt' is denied.", False),
    ("Copy-Item -Destination dst -Path src", r"Access to the path 'C:\work\src' is denied.", False),
    ("Copy-Item -Destination dst -Path src", r"Access to the path 'C:\work\dst' is denied.", True),
    ("Copy-Item dst -Path src", r"Access to the path 'C:\work\src' is denied.", False),
    ("Copy-Item dst -Path src", r"Access to the path 'C:\work\dst' is denied.", True),
    ("Copy-Item src dst", r"Access to the path 'C:\work\src' is denied.", False),
    ("Copy-Item src dst", r"Access to the path 'C:\work\dst' is denied.", True),
    (r"Copy-Item input\dst dst", r"Access to the path 'C:\work\input\dst' is denied.", False),
])
def test_windows_sandbox_hint_for_resolved_path(command, diagnostic, write, workspace_boundary):
    boundary = {**workspace_boundary, "backend": "windows-appcontainer"}
    expected = "tools.output.sandbox_windows_write_hint" if write else None
    assert sandbox_failure_hint(command, diagnostic, boundary, t=str) == expected


@pytest.mark.parametrize(("statement", "write"), [
    ('(Path(d) / "out").write_text("hi")', True),
    ('self.dest.write_text("hi")', True),
    ('open(f"{base}/out", "w")', True),
    ('Path("/tmp/other").write_text("hi")', False),
    ('open(f"{base}/out")', False),
])
def test_python_traceback_uses_failing_statement(statement, write, workspace_boundary):
    diagnostic = (
        'Traceback (most recent call last):\n  File "script.py", line 2, in <module>\n'
        f"    {statement}\nPermissionError: [Errno 13] Permission denied: '/tmp/out'"
    )
    expected = "tools.output.sandbox_tmp_hint" if write else None
    assert sandbox_failure_hint("python script.py", diagnostic, workspace_boundary, t=str) == expected


@pytest.mark.parametrize("underline", ["", "    ~~~~^^^^^^\n"], ids=["plain", "caret"])
def test_python_traceback_read_after_write(workspace_boundary, underline):
    command = shlex.join(["python", "-c", 'open("/tmp/out", "w").close()\nopen(path)'])
    diagnostic = (
        'Traceback (most recent call last):\n  File "<string>", line 2, in <module>\n'
        f"    open(path)\n{underline}PermissionError: [Errno 13] Permission denied: '/tmp/out'"
    )
    assert sandbox_failure_hint(command, diagnostic, workspace_boundary) is None


@pytest.mark.parametrize("expression", [
    "open('/tmp/out', 'a')", "open('/tmp/out', mode='xb')", "Path('/tmp/out').write_bytes(b'data')",
    "Path('/tmp/out').mkdir()", "Path('/tmp/out').unlink()", "Path('old').rename('/tmp/out')",
])
def test_python_mutation_permission_errors(expression, workspace_boundary):
    command = shlex.join(["python", "-c", "from pathlib import Path; " + expression])
    diagnostic = "PermissionError: [Errno 13] Permission denied: '/tmp/out'"
    assert sandbox_failure_hint(command, diagnostic, workspace_boundary, t=str) == "tools.output.sandbox_tmp_hint"


@pytest.mark.parametrize(("code", "status"), [(0, "ok"), (1, "timeout"), (None, "aborted")])
def test_shell_only_appends_write_hint_on_completed_nonzero_exit(project, monkeypatch, workspace_boundary, code, status):
    output = CommandOutput("touch: /tmp/out: Permission denied", boundary=workspace_boundary)
    monkeypatch.setattr("virgo.tools.bash.run_command", lambda *a, **kw: (output, code, status))
    result = make_shell_tool(Shell("bash", "/bin/bash", "3.2")).execute({"command": "touch /tmp/out"}, ctx(project))
    assert result.details["status"] == status
    assert "$TMPDIR" not in result.as_text()


def test_unrestricted_sandbox_omits_write_hint(workspace_boundary):
    workspace_boundary["effective"]["native_process"]["unrestricted"] = True
    assert sandbox_failure_hint("touch /tmp/out", "touch: /tmp/out: Permission denied", workspace_boundary) is None
