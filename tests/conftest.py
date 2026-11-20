from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from virgo.ai.providers import faux
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI
from tests.support.terminal import HAS_PTY


def pytest_collection_modifyitems(config, items):
    if HAS_PTY:
        return
    skip = pytest.mark.skip(reason="needs a POSIX pseudo terminal")
    for item in items:
        if item.get_closest_marker("pty"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("VIRGO_HOME", str(home))
    monkeypatch.delenv("VIRGO_MODEL", raising=False)
    faux.reset()
    yield
    faux.reset()


@pytest.fixture
def project(tmp_path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def python_command(tmp_path):
    """Quote for an explicit selected shell, or the native shell used by hooks/legacy Bash."""
    directory = tmp_path / "python commands"
    directory.mkdir()
    count = 0

    def command(source, *, shell=None):
        nonlocal count
        count += 1
        script = directory / f"command-{count}.py"
        script.write_text(source, encoding="utf-8", newline="\n")
        argv = [sys.executable, str(script)]
        if shell is not None:
            if shell.tool_name == "powershell":
                return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
            return shlex.join(argv)
        return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

    return command


@pytest.fixture
def make_session(project):
    made = []

    def factory(*responses, ui=None, approval="ask", allow=True, persist=False, settings=None, **kw) -> AgentSession:
        faux.script("test", *responses)
        configured = dict(settings or {})
        # Most tests exercise Host command/process behavior directly. Keep the
        # fixture explicit so changing the product default cannot silently turn
        # them into native-sandbox tests; default-mode coverage uses AgentSession.
        # Migration tests opt out by supplying sandbox/permission configuration.
        if not {"execution", "sandbox", "permissions"}.intersection(configured):
            configured["execution"] = {"mode": "no-limit"}
        s = AgentSession(
            kw.pop("cwd", project),
            ui=ui or RecordingUI(),
            model="faux/test",
            session=kw.pop("session", "new" if persist else None),
            approval_policy=approval,
            settings=configured,
            # These local fixtures create their own extension files deliberately.
            trust_project_extensions=kw.pop("trust_project_extensions", True),
            **kw,
        )
        made.append(s)
        return s

    yield factory
    for s in made:
        s.shutdown()


@pytest.fixture
def trash(tmp_path, monkeypatch):
    """Send deleted sessions to a local folder instead of the system trash; record (source, destination)."""
    directory = tmp_path / "trash"
    directory.mkdir()
    moved = []

    def move(path):
        source = Path(path)
        destination = directory / source.name
        source.rename(destination)
        moved.append((source, destination))

    monkeypatch.setattr("virgo.harness.store.send2trash", move)
    return moved


@pytest.fixture
def broker_session(make_session, monkeypatch, tmp_path):
    """Exercise broker decisions without launching an OS sandbox."""
    from virgo.execution import ExecutionUnavailable
    from virgo.sandbox.native import NativeExecutor

    from tests.support.execution import sandbox_config

    monkeypatch.setattr("virgo.sandbox.native.platform_backend", lambda: ("not-launched", tmp_path))
    if sys.platform == "win32":
        monkeypatch.setattr("virgo.sandbox.windows_sandbox.precheck_windows_sandbox", lambda: None)

    def unavailable(*args, **kwargs):
        raise ExecutionUnavailable("Native backend deliberately unavailable in broker test", reason="backend_unavailable")

    monkeypatch.setattr(NativeExecutor, "run_process", unavailable)

    def factory(*responses, preset="workspace-edit", settings=None, **kw):
        from virgo.harness.settings import deep_merge

        return make_session(*responses, settings=deep_merge(sandbox_config(preset), settings or {}), **kw)
    return factory
