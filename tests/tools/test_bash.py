"""The bash tool: exit codes, timeouts, truncation, environment, and failed downloads."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from virgo.agent.types import ToolContext
from virgo.tools import BASH
from virgo.tools import bash as bash_module
from virgo.tools.truncate import MAX_LINES

from tests.support.http import serve


@pytest.fixture(params=["bash", "zsh"])
def shell(request, monkeypatch):
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} is not installed")
    monkeypatch.setattr(bash_module, "SHELL", executable)


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


def run(project, command):
    return BASH.execute({"command": command}, ctx(project))


@pytest.mark.parametrize(("endpoint", "code"), [("timeout", 28), ("error", 22)])
def test_failed_download_does_not_read_existing_file(project, shell, download, endpoint, code):
    old = project / "kv.xml"
    old.write_text("stale-search-result")
    result = run(project, f"{download}/{endpoint} -o kv.xml; cat kv.xml; touch consumed")
    assert result.is_error
    assert result.details["exit_code"] == code
    assert "stale-search-result" not in result.as_text()
    assert not (project / "consumed").exists()
    assert old.read_text() == "stale-search-result"


def test_failed_download_in_pipeline_is_not_reported_as_success(project, shell, download):
    result = run(project, f"{download}/error | cat; touch consumed")
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
    )
    assert not result.is_error
    assert "download failed\nhandled" in result.as_text()


def test_background_download_failure_does_not_read_old_file(make_session, project, shell, download):
    (project / "kv.xml").write_text("stale-search-result")
    session = make_session()
    jobs = session.services["jobs"]
    job = jobs.start_command(f"{download}/timeout -o kv.xml; cat kv.xml; touch consumed", project)
    assert session.ui.woken.wait(5)
    assert job.status == "failed" and job.exit_code == 28
    assert "stale-search-result" not in jobs.output(job)
    assert not (project / "consumed").exists()


def test_bash_exit_codes_timeout_and_truncation(project):
    r = BASH.execute({"command": "echo hi; exit 3"}, ctx(project))
    assert r.is_error and "hi" in r.as_text() and "Exit code 3" in r.as_text()
    assert BASH.execute({"command": "pwd"}, ctx(project)).as_text().strip() == str(project)
    t = BASH.execute({"command": "sleep 5", "timeout": 0.3}, ctx(project))
    assert t.is_error and "timed out" in t.as_text()
    big = BASH.execute({"command": f"seq 1 {MAX_LINES + 100}"}, ctx(project)).as_text()
    assert "Output truncated" in big and big.rstrip().endswith(str(MAX_LINES + 100))
    saved = Path(big.split("Full output: ")[1].split("]")[0])
    assert saved.parent == project / ".virgo" / "outputs"
    assert saved.read_text().startswith("1\n")


def test_bash_marks_child_processes(project):
    result = BASH.execute({"command": 'printf "%s" "$VIRGO"'}, ctx(project))
    assert not result.is_error and result.as_text() == "1"


def test_shell_environment_override(project):
    code = (
        "from pathlib import Path; from virgo.tools.bash import spawn; "
        "proc = spawn(\"import os; print(os.environ['VIRGO'])\", Path.cwd()); "
        "out, _ = proc.communicate(timeout=5); print(out.decode(), end=''); "
        "raise SystemExit(proc.returncode)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project,
        env={**os.environ, "VIRGO_SHELL": sys.executable},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "1"
