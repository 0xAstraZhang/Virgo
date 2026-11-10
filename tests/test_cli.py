"""Exercise installed command, module and checkout entry points in fresh processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMAND = Path(sysconfig.get_path("scripts")) / ("virgo.exe" if os.name == "nt" else "virgo")
ENTRY_POINTS = [[str(COMMAND)], [sys.executable, "-m", "virgo"], [sys.executable, str(ROOT / "main.py")]]


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module", "checkout"])
def test_cli_identity(entry, project):
    result = subprocess.run(
        [*entry, "--version"], cwd=project, capture_output=True, text=True, check=True, timeout=10
    )
    assert result.stdout.strip() == f"virgo {version('virgo')}"
    help_result = subprocess.run(
        [*entry, "--help"], cwd=project, capture_output=True, text=True, check=True, timeout=10
    )
    assert help_result.stdout.startswith("usage: virgo ")


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module", "checkout"])
def test_cli_loads_features_and_streams_json(entry, project):
    (project / "sample.py").write_text('print("hello")\n')
    result = subprocess.run(
        [
            *entry,
            "--no-session",
            "--no-extensions",
            "--model",
            "faux/demo",
            "-p",
            "--mode",
            "json",
            "find python files",
        ],
        cwd=project,
        input="",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events and "sample.py" in result.stdout
