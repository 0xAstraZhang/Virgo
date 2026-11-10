from __future__ import annotations

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.features.permission import is_read_only_command, rule_matches

from .conftest import RecordingUI


def results(session):
    return [m for m in session.main.messages if isinstance(m, ToolResultMessage)]


def test_read_only_command_classifier():
    for cmd in [
        "ls -la",
        "git status && git diff HEAD",
        "cat a.txt | grep x | wc -l",
        "FOO=1 rg pattern",
        "find . -name '*.py'",
    ]:
        assert is_read_only_command(cmd), cmd
    for cmd in [
        "rm x",
        "echo hi > f",
        "git push",
        "ls; rm -rf build",
        "find . -delete",
        "sed -i s/a/b/ f",
        "cat $(whoami)",
        "python x.py",
    ]:
        assert not is_read_only_command(cmd), cmd


def test_default_mode_asks_and_respects_answers(make_session, project):
    ui = RecordingUI(answers=["n", "y"])
    s = make_session(
        faux.reply(tools=[("write", {"path": "a.txt", "content": "1"})]),
        faux.reply(tools=[("write", {"path": "a.txt", "content": "2"})]),
        faux.reply(tools=[("read", {"path": "a.txt"}), ("bash", {"command": "git status"})]),
        faux.reply("done"),
        ui=ui,
        mode="default",
    )
    s.prompt("go")
    r = results(s)
    assert r[0].is_error and "denied" in r[0].text()
    assert not r[1].is_error and (project / "a.txt").read_text() == "2"
    assert not r[2].is_error and len(ui.questions) == 2  # read + read-only bash never ask


def test_always_answer_is_remembered(make_session):
    ui = RecordingUI(answers=["a"])
    s = make_session(
        faux.reply(tools=[("bash", {"command": "make build"})]),
        faux.reply(tools=[("bash", {"command": "make build --fast"})]),
        faux.reply("done"),
        ui=ui,
        mode="default",
    )
    s.prompt("go")
    assert len(ui.questions) == 1
    assert all(not r.text().startswith("The user denied") for r in results(s))


def test_plan_mode_and_hard_deny_and_rules(make_session):
    s = make_session(
        faux.reply(
            tools=[
                ("write", {"path": "x", "content": ""}),
                ("bash", {"command": "ls"}),
                ("bash", {"command": "sudo reboot"}),
            ]
        ),
        faux.reply("ok"),
        mode="plan",
    )
    s.prompt("go")
    r = results(s)
    assert r[0].is_error and "Plan mode" in r[0].text()
    assert not r[1].is_error
    assert r[2].is_error and "safety list" in r[2].text()

    s2 = make_session(
        faux.reply(tools=[("bash", {"command": "touch made.txt"}), ("bash", {"command": "npm publish"})]),
        faux.reply("ok"),
        mode="default",
        settings={"permissions": {"allow": ["bash(touch *)"], "deny": ["bash(npm publish*)"]}},
    )
    s2.prompt("go")
    r = results(s2)
    assert not r[0].is_error and r[1].is_error and "rule" in r[1].text()


def test_unattended_asks_are_denied(make_session):
    class Headless(RecordingUI):
        def confirm(self, q, agent="main"):
            return None

    s = make_session(
        faux.reply(tools=[("bash", {"command": "make"})]), faux.reply("ok"), ui=Headless(), mode="default"
    )
    s.prompt("go")
    assert "nobody is available" in results(s)[0].text()


def test_write_outside_workspace_asks_even_in_accept_edits(make_session, tmp_path):
    ui = RecordingUI(answers=["n"])
    s = make_session(
        faux.reply(
            tools=[
                ("write", {"path": "inside.txt", "content": ""}),
                ("write", {"path": str(tmp_path / "outside.txt"), "content": ""}),
            ]
        ),
        faux.reply("ok"),
        ui=ui,
        mode="accept-edits",
    )
    s.prompt("go")
    r = results(s)
    assert not r[0].is_error and r[1].is_error and len(ui.questions) == 1


def test_rule_matching_paths(project):
    from virgo.agent.types import ToolCallEvent

    ev = ToolCallEvent("1", "edit", {"path": "src/app/main.py"}, None, "main", project)
    assert rule_matches("edit(src/**)", ev)
    assert rule_matches("edit", ev)
    assert not rule_matches("write(src/**)", ev)
    assert rule_matches("mcp__*", ToolCallEvent("1", "mcp__gh__issue", {}, None, "main", project))
