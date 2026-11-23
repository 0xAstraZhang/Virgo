"""Exercise the installed command and module entry points in fresh processes."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from contextlib import suppress
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from virgo.ai import resolve_model
from virgo.ai.catalog import CatalogError, CatalogModel
from virgo.ai.model_metadata import cache_path, write_metadata
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, Usage
from virgo.harness import preferences

from tests.support.harness import TORN_MESSAGE, install_responses, wait_until, write_legacy_session
from tests.support.persistence import diagnostic_records

COMMAND = Path(sysconfig.get_path("scripts")) / ("virgo.exe" if os.name == "nt" else "virgo")
ENTRY_POINTS = [[str(COMMAND)], [sys.executable, "-m", "virgo"]]


def test_windows_home_workspace_selection(tmp_path, monkeypatch):
    cli = import_module("virgo.cli.main")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(cli, "AgentSession", lambda cwd, **kwargs: seen.append(Path(cwd).resolve()))
    monkeypatch.setattr("virgo.cli.repl.run_repl", lambda *args: None)
    workspace = tmp_path / "VirgoWorkspace"
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0 and not workspace.exists()
    assert cli.main(["--ui", "plain"]) == 0
    assert workspace.is_dir()
    assert cli.main(["--ui", "plain"]) == 0
    assert cli.main(["--ui", "plain", "-C", "."]) == 0
    monkeypatch.chdir(workspace)
    assert cli.main(["--ui", "plain"]) == 0
    assert seen == [workspace, workspace, tmp_path, workspace]


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
         "--format", "json", "--permission", "no-limit", "/permissions"],
        cwd=project, input="", capture_output=True, text=True, check=True, timeout=10,
    )
    assert "Host" in result.stdout
    assert all(isinstance(json.loads(line), dict) for line in result.stdout.splitlines())
    for flag, value in (
        ("--work-mode", "plan"),
        ("--plan", None),
        ("--profile", "minimal"),
        ("--execution", "host"),
        ("--sandbox", "disabled"),
        ("--mode", "json"),
    ):
        rejected = subprocess.run(
            [*entry, "--no-session", flag, *([value] if value else [])],
            cwd=project, input="", capture_output=True, text=True, timeout=10,
        )
        assert rejected.returncode == 2 and "unrecognized arguments" in rejected.stderr


@pytest.mark.parametrize("language_args", [[], ["--language", "en"]])
def test_cli_applies_execution_override_before_validating_user_preset(project, monkeypatch, capsys, language_args):
    cli = import_module("virgo.cli.main")
    home = Path(os.environ["VIRGO_HOME"])
    executor = SimpleNamespace(backend="not-launched", close=lambda: None, close_idle=lambda: None)
    monkeypatch.setattr("virgo.harness.execution.SandboxExecutor", lambda: executor)
    if sys.platform == "win32":
        monkeypatch.setattr("virgo.sandbox.windows_sandbox.precheck_windows_sandbox", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    # S7-02: Retired permissions.preset fails clearly with exit code 2 instead of silent migration.
    (home / "settings.json").write_text(json.dumps({"language": "zh-CN", "permissions": {"preset": "workspace-edit"}}))
    rejected = cli.main(["-C", str(project), "--permission", "workspace-edit", "--no-session", "-m", "faux/demo",
                         *language_args, "-p", "/permissions show"])
    assert rejected == 2

    # Valid user settings accept --permission override cleanly.
    (home / "settings.json").write_text(json.dumps({"language": "zh-CN", "execution": {"mode": "no-limit"}}))
    result = cli.main(["-C", str(project), "--permission", "workspace-edit", "--no-session", "-m", "faux/demo",
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


@pytest.mark.skipif(os.name != "posix", reason="requires SIGINT delivery to a PID and POSIX process groups")
def test_print_sigint_cancels_parallel_shell_process_groups(project, python_command, monkeypatch):
    monkeypatch.setenv("VIRGO_SHELL", "/bin/bash")
    payload = python_command(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "slot = sys.argv[1]\n"
        "Path(f'started-{slot}-{os.getpid()}').touch()\n"
        "time.sleep(20)\n"
        "Path(f'late-{slot}').touch()\n"
    )
    calls = [("bash", {"command": f"exec {payload} {slot}"}) for slot in ("one", "two")]
    # Only the provider is scripted. The second process must see the saved tool calls.
    driver = f"""
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage
from virgo.cli.main import main

def respond(ctx):
    if ctx.messages[-1].text() == "start":
        return faux.reply(tools={calls!r})
    if any(isinstance(m, AssistantMessage) and len(m.tool_calls()) == 2 for m in ctx.messages):
        return faux.reply("resumed interrupted batch")
    return faux.error("missing interrupted batch in history")

faux.handler("interrupt", respond)
raise SystemExit(main())
"""
    argv = [sys.executable, "-c", driver, "-p", "-m", "faux/interrupt", "--permission", "no-limit"]

    def started_pids():
        return {int(path.name.rsplit("-", 1)[1]) for path in project.glob("started-*")}

    reaped = set()
    with subprocess.Popen(
        [*argv, "start"],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as proc:
        try:

            def both_started():
                return all(any(project.glob(f"started-{slot}-*")) for slot in ("one", "two"))

            wait_until(lambda: both_started() or proc.poll() is not None, timeout=15)
            assert proc.poll() is None, proc.stdout.read()
            pids = started_pids()
            assert len(pids) == 2
            for pid in pids:
                assert os.getpgid(pid) == pid  # exec made the payload the live group leader

            start = time.monotonic()
            proc.send_signal(signal.SIGINT)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail("Virgo was still running 5.0s after SIGINT with both shell calls started")
            assert time.monotonic() - start < 5.0
            assert code == 130, proc.stdout.read()
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
                with pytest.raises(ProcessLookupError):
                    os.killpg(pid, 0)
                reaped.add(pid)
            assert not list(project.glob("late-*"))

            resumed = subprocess.run(
                [*argv, "--continue", "resume"],
                cwd=project,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            assert resumed.stdout.strip() == "resumed interrupted batch"
            assert started_pids() == pids
            assert not list(project.glob("late-*"))
        finally:
            # A failed cancellation (including replay on resume) must not leave a payload running.
            for pid in started_pids() - reaped:
                with suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            proc.kill()


@pytest.mark.parametrize(("responses", "expected"), [
    pytest.param([faux.reply("inter", stop_reason="length"), faux.reply("face")],
                 "interface", id="one-continuation"),
    pytest.param([faux.reply("def get_", stop_reason="length"), faux.reply("value():\n", stop_reason="length"),
                  faux.reply("    return 42")], "def get_value():\n    return 42", id="two-continuations"),
    pytest.param([faux.reply(thinking="reasoning", stop_reason="length"), faux.reply("done")],
                 "done", id="thinking-only"),
    pytest.param([faux.reply("Looking for files", stop_reason="length"),
                  faux.reply(tools=[("glob", {"pattern": "*"})]), faux.reply("Found the files.")],
                 "Found the files.", id="tool-result"),
])
def test_print_text_after_length_continuation(project, monkeypatch, capsys, responses, expected):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    faux.script("limited", *responses)
    code = cli.main(["-p", "--format", "text", "--no-session", "-C", str(project),
                     "--permission", "no-limit", "-m", "faux/limited", "hello"])
    assert code == 0
    assert capsys.readouterr().out == expected + "\n"


def test_print_text_after_auto_compaction_of_length_fragment(project, monkeypatch, capsys):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    (Path(os.environ["VIRGO_HOME"]) / "settings.json").write_text(json.dumps({
        "compaction": {"triggerTokens": 50_000, "keepRecentTokens": 1},
    }))
    seen = install_responses(
        monkeypatch,
        AssistantMessage([TextContent("inter")], stop_reason="length", usage=Usage(input=50_000)),
        AssistantMessage([TextContent("Earlier progress")]),
        AssistantMessage([TextContent("face")]),
    )
    code = cli.main(["-p", "--format", "text", "-C", str(project),
                     "--permission", "no-limit", "-m", "faux/limited", "hello"])

    continuation = seen[-1][1].messages
    assert continuation[0].kind == "summary"
    assert not any(isinstance(message, AssistantMessage) for message in continuation)
    assert code == 0
    assert capsys.readouterr().out == "interface\n"


@pytest.mark.parametrize(("fmt", "language", "partial", "prefix", "warning"), [
    ("text", "en", "unfinished", "virgo: ", "output token limit"),
    ("json", "zh-CN", "", "virgo：", "输出 token 上限"),
])
def test_print_reports_incomplete_response_after_length_continuations(project, monkeypatch, capsys,
                                                                       fmt, language, partial, prefix, warning):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    truncated = faux.reply(partial, thinking="reasoning", stop_reason="length")
    faux.script("limited", truncated, truncated, truncated)
    code = cli.main(["-p", "--format", fmt, "--language", language, "--no-session", "-C", str(project),
                     "--permission", "no-limit", "-m", "faux/limited", "hello"])
    output = capsys.readouterr()
    assert code == 1
    assert len(faux.CALLS["limited"]) == 3
    assert prefix in output.err and warning in output.err
    if fmt == "text":
        assert output.out == ""
    else:
        events = [json.loads(line) for line in output.out.splitlines()]
        assert events[-1]["type"] == "agent_end"


@pytest.mark.parametrize(("flag", "fmt"), [("-p", "text"), ("--print", "json")])
def test_print_uses_cached_model_catalog_without_refresh(flag, fmt, project, monkeypatch, capsys):
    cli = import_module("virgo.cli.main")
    model = resolve_model("deepseek/deepseek-flash")
    home = cli.Paths(project).user
    write_metadata(cache_path(home, model), [CatalogModel(model.id, {"context_window": 234567})])
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-test-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    refresh = Mock(wraps=preferences.start_model_refresh)
    fetch = Mock(side_effect=CatalogError("Model directory returned HTTP 401.", 401))
    monkeypatch.setattr(preferences, "start_model_refresh", refresh)
    monkeypatch.setattr(preferences, "fetch_model_catalog", fetch)

    assert cli.main([flag, "--format", fmt, "-C", str(project), "-m", model.spec,
                     "--permission", "no-limit", "/status"]) == 0
    output = capsys.readouterr()
    assert "234,567" in output.out
    assert output.err == ""
    assert diagnostic_records(home / "logs" / "diagnostics.sqlite3") == []
    refresh.assert_not_called()
    fetch.assert_not_called()


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "term", "platform", "indicators", "expected"),
    [
        (True, True, "xterm-256color", "linux", {}, True),
        (False, True, "xterm-256color", "linux", {}, False),
        (True, False, "xterm-256color", "linux", {}, False),
        (True, True, None, "win32", {"WT_SESSION": "session-id"}, True),
        (True, True, "", "win32", {"WT_SESSION": "session-id"}, True),
        (False, True, None, "win32", {"WT_SESSION": "session-id"}, False),
        (True, False, None, "win32", {"WT_SESSION": "session-id"}, False),
        (True, True, "dumb", "win32", {"WT_SESSION": "session-id"}, False),
        (True, True, "unknown", "win32", {"TERM_PROGRAM": "vscode"}, False),
        (True, True, None, "win32", {"TERM_PROGRAM": "vscode"}, True),
        (True, True, "", "win32", {"TERM_PROGRAM": "WezTerm"}, True),
        (True, True, None, "win32", {"TERM_PROGRAM": "Dumb"}, False),
        (True, True, None, "win32", {"TERM_PROGRAM": "UNKNOWN"}, False),
        (True, True, None, "win32", {"TERMINAL_EMULATOR": "JetBrains-JediTerm"}, True),
        (True, True, None, "win32", {"ConEmuANSI": "on"}, True),
        (True, True, None, "win32", {"ConEmuANSI": "OFF"}, False),
        (True, True, None, "win32", {"ANSICON": "1"}, True),
        (True, True, None, "win32", {}, False),
        (True, True, None, "linux", {"WT_SESSION": "session-id"}, False),
    ],
)
def test_fullscreen_terminal_detection(monkeypatch, stdin_tty, stdout_tty, term, platform, indicators, expected):
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
    monkeypatch.setattr(sys, "platform", platform)
    for name in ("WT_SESSION", "TERM_PROGRAM", "TERMINAL_EMULATOR", "ConEmuANSI", "ANSICON"):
        monkeypatch.delenv(name, raising=False)
    for name, value in indicators.items():
        monkeypatch.setenv(name, value)
    if term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", term)
    assert cli._fullscreen_available() is expected


@pytest.mark.parametrize(
    ("platform", "term", "indicators", "vt_supported", "expected"),
    [
        ("win32", None, {}, True, True),
        ("win32", "", {}, True, True),
        ("win32", None, {}, False, False),
        ("win32", "dumb", {}, True, False),
        ("win32", None, {"TERM_PROGRAM": "dumb"}, True, False),
        ("win32", None, {"TERM_PROGRAM": "unknown"}, True, False),
        ("linux", None, {}, True, False),
    ],
)
def test_fullscreen_windows_vt_console_fallback(monkeypatch, platform, term, indicators, vt_supported, expected):
    import io

    cli = import_module("virgo.cli.main")

    class Stream(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Stream())
    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(cli, "_windows_console_supports_vt", lambda: vt_supported)
    for name in ("WT_SESSION", "TERM_PROGRAM", "TERMINAL_EMULATOR", "ConEmuANSI", "ANSICON"):
        monkeypatch.delenv(name, raising=False)
    for name, value in indicators.items():
        monkeypatch.setenv(name, value)
    if term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", term)
    assert cli._fullscreen_available() is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 console mode probe")
@pytest.mark.parametrize(
    ("in_init", "out_init", "fail_on", "expected", "expected_sets"),
    [
        (0x0207, 0x0007, None, True, []),
        (0x0007, 0x0003, None, True, [(200, 0x0007), (100, 0x0207), (100, 0x0007), (200, 0x0003)]),
        (0x0007, 0x0003, 100, False, [(200, 0x0007), (100, 0x0207), (200, 0x0003)]),
        (0x0007, 0x0003, 200, False, [(200, 0x0007)]),
        (None, 0x0003, None, False, []),
    ],
    ids=["already-vt", "enable-and-restore", "stdin-vt-fails-restores-stdout", "stdout-vt-fails", "not-console"],
)
def test_windows_console_vt_probe_restores_modes(monkeypatch, in_init, out_init, fail_on, expected, expected_sets):
    import ctypes
    import msvcrt

    cli = import_module("virgo.cli.main")
    sets: list[tuple[int, int]] = []
    modes = {100: in_init, 200: out_init}

    class FakeStream:
        def __init__(self, fd):
            self._fd = fd

        def fileno(self):
            return self._fd

    class FakeFn:
        def __init__(self, fn):
            self._fn = fn

        def __call__(self, *args):
            return self._fn(*args)

    def fake_get_mode(handle, out_ptr):
        value = modes.get(handle)
        if value is None:
            return 0
        out_ptr._obj.value = value
        return 1

    def fake_set_mode(handle, mode):
        sets.append((handle, mode))
        return 0 if handle == fail_on else 1

    fake_kernel32 = SimpleNamespace(
        GetConsoleMode=FakeFn(fake_get_mode),
        SetConsoleMode=FakeFn(fake_set_mode),
    )

    monkeypatch.setattr(sys, "stdin", FakeStream(10))
    monkeypatch.setattr(sys, "stdout", FakeStream(20))
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: 100 if fd == 10 else 200)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_kw: fake_kernel32)
    assert cli._windows_console_supports_vt() is expected
    assert sets == expected_sets


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
    assert calls["kwargs"]["refresh_models"] is True
    assert calls["run"] == (session, calls["kwargs"]["ui"], "initial prompt", pick_session)


@pytest.mark.parametrize(("mode", "damage"), [
    ("continue", "torn"), ("path", "torn"), ("continue", "corrupt"), ("path", "corrupt"), ("continue", "empty-dir"),
])
def test_print_resume_reports_recovery_on_stderr(project, monkeypatch, capsys, mode, damage):
    cli = import_module("virgo.cli.main")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    directory = cli.Paths(project).sessions
    path = directory / "latest.jsonl"
    directory.mkdir(parents=True)
    if damage != "empty-dir":
        older = directory / "older.jsonl"
        write_legacy_session(older, project, "2025-01-01")
        os.utime(older, (100, 100))
        write_legacy_session(path, project, "2025-01-02", "prior question")
        original = path.read_bytes() + TORN_MESSAGE + (b"\n" if damage == "corrupt" else b"")
        path.write_bytes(original)
    faux.script("test", faux.reply("answer"))
    arg = ["--continue"] if mode == "continue" else ["-r", str(path)]
    code = cli.main(["-p", "--format", "json", "--language", "en", "--permission", "no-limit",
                     "-C", str(project), "-m", "faux/test", *arg, "next question"])
    output = capsys.readouterr()
    if damage == "corrupt":
        assert code == 2 and output.out == "" and not faux.CALLS["test"]
        assert str(path) in output.err and "line 3" in output.err
        assert path.read_bytes() == original and set(directory.glob("*.jsonl")) == {path, older}
        return
    assert code == 0
    events = [json.loads(line) for line in output.out.splitlines()]
    assert events[0]["type"] == "agent_start" and events[-1]["type"] == "agent_end"
    if damage == "empty-dir":
        assert output.err == "" and len(list(directory.glob("*.jsonl"))) == 1
    else:
        warning, = output.err.splitlines()
        assert "line 3" in warning and "83 bytes" in warning and "user" in warning
        assert [msg["content"] for msg in faux.CALLS["test"][0]["messages"]] == ["prior question", "next question"]
        assert cli.SessionStore.load(path).id == "latest" and set(directory.glob("*.jsonl")) == {path, older}
