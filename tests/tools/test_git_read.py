"""Controlled Git transport failure recovery with real inherited pipes."""

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from virgo.tools.git_read import ControlledGit, GitReadError


def test_inherited_pipe_cleanup_preserves_cancellation(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    cancel = threading.Event()
    git = ControlledGit(tmp_path, cancel)
    git.argv = [sys.executable, "-c", (
        "import subprocess,sys; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"Path({str(marker)!r}).write_text(str(p.pid))"
    )]
    processes = []
    popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("virgo.tools.git_read.subprocess.Popen", capture)

    def stop_after_dispatch():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists() and processes and processes[0].poll() is not None:
                break
            time.sleep(0.01)
        cancel.set()

    stopper = threading.Thread(target=stop_after_dispatch, daemon=True)
    stopper.start()
    try:
        with pytest.raises(GitReadError) as caught:
            git.run()
        assert caught.value.status == "cancelled"
        assert "cleanup" in " ".join(caught.value.__notes__).lower()
        assert all(stream.closed for stream in (processes[0].stdout, processes[0].stderr))
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        stopper.join(timeout=5)
