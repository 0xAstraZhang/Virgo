"""Exercise the installed command and module entry points in fresh processes."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import sysconfig
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

COMMAND = Path(sysconfig.get_path("scripts")) / ("virgo.exe" if os.name == "nt" else "virgo")
ENTRY_POINTS = [[str(COMMAND)], [sys.executable, "-m", "virgo"]]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("auto-review", id="auto-review-auto-review"),
        pytest.param("accept-all", id="accept-all-accept-all"),
        pytest.param("plan", id="plan-plan"),
        pytest.param("default", id="default-auto-review"),
        pytest.param("accept-edits", id="accept-edits-auto-review"),
        pytest.param("yolo", id="yolo-accept-all"),
    ],
)
def test_permission_mode_arguments_and_legacy_aliases(value):
    cli = import_module("virgo.cli.main")
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--permission-mode", value])
    assert parser.parse_args(["--approval", "ask"]).approval == "ask"
    assert parser.parse_args(["--approval", "never"]).approval == "never"
    assert "--permission-mode" not in parser.format_help()


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module"])
def test_removed_task_flags_are_rejected_and_permission_output_stays_independent(entry, project):
    result = subprocess.run(
        [*entry, "--no-session", "--no-extensions", "--model", "faux/demo", "-p",
         "--format", "json", "--execution", "host", "/permissions"],
        cwd=project, input="", capture_output=True, text=True, check=True, timeout=10,
    )
    assert "Host" in result.stdout
    assert all(isinstance(json.loads(line), dict) for line in result.stdout.splitlines())
    for flag, value in (("--work-mode", "plan"), ("--plan", None), ("--profile", "minimal")):
        rejected = subprocess.run(
            [*entry, "--no-session", flag, *([value] if value else [])],
            cwd=project, input="", capture_output=True, text=True, timeout=10,
        )
        assert rejected.returncode == 2 and "unrecognized arguments" in rejected.stderr
    # The retired output flag must not become an abbreviation of --model.
    legacy = subprocess.run(
        [*entry, "--no-session", "--no-extensions", "--mode", "json",
         "--model", "faux/demo", "-p", "/permissions"],
        cwd=project, input="", capture_output=True, text=True, timeout=10,
    )
    assert legacy.returncode == 2
    assert "requires execution.provider=sandbox" in legacy.stderr and not legacy.stdout


@pytest.mark.parametrize("language_args", [[], ["--language", "en"]])
def test_cli_applies_execution_override_before_validating_user_preset(project, monkeypatch, capsys, language_args):
    cli = import_module("virgo.cli.main")
    home = Path(os.environ["VIRGO_HOME"])
    executor = SimpleNamespace(path=home / "mxc", backend="not-launched")
    monkeypatch.setattr("virgo.extensions.sandbox.MxcExecutor", lambda _: executor)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    (home / "settings.json").write_text(json.dumps({"language": "zh-CN", "permissions": {"preset": "read-only"}}))

    result = cli.main(["-C", str(project), "--execution", "sandbox", "--no-session", "-m", "faux/demo",
                       *language_args, "-p", "/permissions show"])

    assert result == 0
    assert '"provider": "sandbox"' in capsys.readouterr().out


def test_localized_help_works_before_migrating_execution_settings(project, capsys):
    cli = import_module("virgo.cli.main")
    (Path(os.environ["VIRGO_HOME"]) / "settings.json").write_text(
        json.dumps({"permissions": {"preset": "read-only"}})
    )
    (project / ".virgo").mkdir()
    (project / ".virgo/settings.json").write_text(json.dumps({"language": "zh-CN"}))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["-C", str(project), "--help"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith(cli.build_parser("zh-CN").format_usage().rstrip())


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module"])
def test_cli_identity(entry, project):
    result = subprocess.run(
        [*entry, "--version"], cwd=project, capture_output=True, text=True, check=True, timeout=10
    )
    assert result.stdout.strip() == f"virgo {version('virgo')}"
    help_result = subprocess.run(
        [*entry, "--help"], cwd=project, capture_output=True, text=True, check=True, timeout=10
    )
    assert help_result.stdout.startswith("usage: virgo ")


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module"])
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
            "--ui",
            "fullscreen",
            "--format",
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


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module"])
def test_fullscreen_rejects_non_terminal_before_starting_session(entry, project):
    result = subprocess.run(
        [*entry, "--ui", "fullscreen", "--model", "faux/demo"],
        cwd=project,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "--ui plain or -p" in result.stderr
    assert not result.stdout
    assert not (Path(os.environ["VIRGO_HOME"]) / "sessions").exists()


@pytest.mark.parametrize("ui", ["auto", "plain"])
@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module"])
def test_non_terminal_uses_plain_repl(entry, ui, project):
    result = subprocess.run(
        [*entry, "--ui", ui, "--no-session", "--no-extensions", "--model", "faux/demo"],
        cwd=project,
        input="/exit\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "Virgo" in result.stdout and "bye" in result.stdout
    assert "\x1b[?1049h" not in result.stdout


@pytest.mark.parametrize("entry", ENTRY_POINTS, ids=["command", "module"])
def test_print_text_ignores_fullscreen_option(entry, project):
    result = subprocess.run(
        [*entry, "--ui", "fullscreen", "-p", "--no-session", "--no-extensions", "-m", "faux/demo"],
        cwd=project,
        input="hello",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "(faux) You said: hello"


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "term", "expected"),
    [
        (True, True, "xterm-256color", True),
        (False, True, "xterm-256color", False),
        (True, False, "xterm-256color", False),
        (True, True, "dumb", False),
        (True, True, "unknown", False),
        (True, True, "", False),
    ],
)
def test_fullscreen_terminal_detection(monkeypatch, stdin_tty, stdout_tty, term, expected):
    import io

    cli = import_module("virgo.cli.main")

    class Stream(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self.tty = tty

        def isatty(self):
            return self.tty

    monkeypatch.setattr(sys, "stdin", Stream(stdin_tty))
    monkeypatch.setattr(sys, "stdout", Stream(stdout_tty))
    monkeypatch.setenv("TERM", term)
    assert cli._fullscreen_available() is expected


@pytest.mark.parametrize("ui", ["auto", "fullscreen"])
@pytest.mark.parametrize(
    ("session_args", "expected_session", "pick_session"),
    [
        ([], "new", False),
        (["-r"], "new", True),
        (["-r", "saved.jsonl"], Path("saved.jsonl"), False),
        (["-c", "-r"], "continue", False),
        (["--no-session", "-r"], None, False),
    ],
)
def test_fullscreen_route_and_resume_picker(monkeypatch, ui, session_args, expected_session, pick_session):
    cli = import_module("virgo.cli.main")
    calls = {}
    session = object()
    bridge = ModuleType("virgo.cli.tui.bridge")
    bridge.FullscreenUI = object
    tui = ModuleType("virgo.cli.tui")

    def make_session(cwd, **kwargs):
        calls["kwargs"] = kwargs
        return session

    def run_fullscreen(actual_session, actual_ui, *, initial, pick_session):
        calls["run"] = (actual_session, actual_ui, initial, pick_session)
        return 17

    tui.run_fullscreen = run_fullscreen
    monkeypatch.setitem(sys.modules, "virgo.cli.tui", tui)
    monkeypatch.setitem(sys.modules, "virgo.cli.tui.bridge", bridge)
    monkeypatch.setattr(cli, "AgentSession", make_session)
    monkeypatch.setattr(cli, "_fullscreen_available", lambda: True)
    monkeypatch.setattr(cli, "_pick_session", lambda _: pytest.fail("plain picker must not run"))

    assert cli.main(["--ui", ui, "initial prompt", *session_args]) == 17
    assert calls["kwargs"]["session"] == expected_session
    assert calls["run"] == (session, calls["kwargs"]["ui"], "initial prompt", pick_session)
