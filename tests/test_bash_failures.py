"""Failed downloads must not be hidden by later commands or pipeline consumers."""

from __future__ import annotations

import shlex
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from virgo.agent.types import ToolContext
from virgo.tools import BASH
from virgo.tools import bash as bash_module


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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    # Ignore personal curl configuration and proxy settings; all traffic stays local.
    yield f"{shlex.quote(curl)} -q --noproxy '*' -fsSL --max-time 0.2 http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def run(project, command):
    return BASH.execute({"command": command}, ToolContext("download", project, threading.Event()))


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
