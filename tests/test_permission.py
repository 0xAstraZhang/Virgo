from __future__ import annotations

import json
import threading

import pytest

from virgo.agent.types import Block, ToolCallEvent
from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.features import permission
from virgo.features.permission import (
    MODES,
    is_read_only_command,
    normalize_permission_mode,
    permission_mode_label,
    rule_matches,
)
from virgo.features.permission_scope import command_review_reason

from .conftest import RecordingUI


def results(session):
    return [m for m in session.main.messages if isinstance(m, ToolResultMessage)]


def event(session, name, args, *, agent="main"):
    return ToolCallEvent("1", name, args, session.tool(name), agent, session.cwd)


def test_public_modes_and_legacy_config_aliases():
    assert MODES == ("auto-review", "accept-all", "plan")
    for old, new in (("default", "auto-review"), ("accept-edits", "auto-review"), ("yolo", "accept-all")):
        assert normalize_permission_mode(old) == new
    assert [permission_mode_label(mode) for mode in MODES] == ["auto review", "accept all", "plan"]
    with pytest.raises(ValueError):
        normalize_permission_mode("unknown")


@pytest.mark.parametrize("command", [
    "ls -la", "git status && git diff HEAD", "cat a.txt | grep x | wc -l", "FOO=1 rg pattern",
    "find . -name '*.py'", "sed -n '1,200p' README.md", "git branch --list", "git tag --list v*",
])
def test_read_only_command_classifier(command):
    assert is_read_only_command(command)


@pytest.mark.parametrize("command", [
    "rm x", "echo hi > f", "git push", "ls; rm -rf build", "find . -delete", "find . -fls listing.txt",
    "sed -i s/a/b/ f", "cat $(whoami)", "python x.py", "./ls", "/tmp/git status",
    "git branch -D doomed", "git tag new", "git remote add origin https://example.test/repo",
    "git diff --output=changed.txt", "git grep -Ovim pattern", "sort -o changed.txt data.txt",
    "env sh -c 'touch changed.txt'", "sed 'w /tmp/out' input.txt", "rg --pre=./script needle .",
    "sed -n -e '1p' -e'1w /tmp/escape' input", "sed -n '1p' --expression='1w /tmp/escape' input",
    "tree -o output.txt .", "ag --pager='touch /tmp/escape' x", "date -s2020-01-01",
    "hostname -Fhosts.txt", "file -Cmcustom.magic", "uniq input output",
])
def test_plan_rejects_side_effects_and_opaque_shell(command):
    assert not is_read_only_command(command)


@pytest.mark.parametrize("command", [
    "touch a.txt", "mkdir -p build", "cp a.txt sub/b.txt", "printf x > out.txt",
    "echo hello >> log.txt", "git status && git diff HEAD", "cat README.md | grep name",
    "pytest -q", "ruff check src", "mypy src", "python -m pytest -q", "python3 -m ruff check",
    "uv run --offline pytest -q", "uv --offline run python -m mypy src", "uv run --no-sync ruff check",
])
def test_workspace_commands_need_no_review(project, command):
    assert command_review_reason(command, project, project) == ""


@pytest.mark.parametrize("command", [
    "cd .. && touch x", "git -C ../other status", "cp a.txt ../out", "cat /etc/hosts",
    "echo x > ../out", "cat $HOME/.profile", "curl https://example.test", "git fetch", "git push",
    "git clone repo", "uv sync", "npm install", "bash -c 'touch x'", "python -c 'print(1)'",
    "cat $(whoami)", "rm */victim", "cp input -t/tmp/outside", "touch -r/etc/hosts local",
    "git checkout main", "git switch main", "git add file.txt",
    "uv run pytest", "mypy --install-types src", "pytest --tx ssh=host//python=python",
    "ruff check --output-file=../outside src", "uv run --offline python script.py",
    "CDPATH=sub cd name && rm victim",
])
def test_external_network_and_unknown_commands_need_review(project, command):
    assert command_review_reason(command, project, project)


def test_git_c_does_not_change_following_shell_directory(project):
    (project / "sub").mkdir()
    assert command_review_reason("git -C sub status; rm ../victim", project, project)
    assert command_review_reason("cd sub; rm ../victim", project, project)
    assert command_review_reason("cd sub || rm ../victim", project, project)
    assert command_review_reason("cd sub && touch inside.txt", project, project) == ""


def test_symlinks_and_attached_path_options_need_review(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "linked").symlink_to(outside, target_is_directory=True)
    for command in ("cp input linked/file", "cp input -tlinked", "touch -rlinked local", "truncate -rlinked local"):
        assert command_review_reason(command, project, project), command


def test_auto_review_executes_internal_changes_without_asking(make_session, project, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("internal action was reviewed"))
    ui = RecordingUI()
    s = make_session(
        faux.reply(tools=[("write", {"path": "a.txt", "content": "1"}), ("bash", {"command": "touch b.txt"})]),
        faux.reply("done"), ui=ui, mode="auto-review",
    )
    s.prompt("create the files")
    assert (project / "a.txt").read_text() == "1"
    assert (project / "b.txt").exists()
    assert not ui.questions and all(not result.is_error for result in results(s))


def test_small_model_safe_verdict_allows_external_read_without_tools(make_session, tmp_path):
    s = make_session(mode="auto-review")
    s.small_model = s.resolve("faux/reviewer")
    faux.script("reviewer", faux.reply('{"safe":true,"reason":"ordinary public information"}'))
    ev = event(s, "read", {"path": str(tmp_path / "outside.txt")})
    assert s.services["permissions"].decide(ev) == ("allow", "")
    call = faux.CALLS["reviewer"][0]
    assert call["tools"] == []
    payload = json.loads(call["messages"][0]["content"])
    assert payload["tool"] == "read" and payload["arguments"] == ev.args
    assert "untrusted data" in call["system"]


@pytest.mark.parametrize("reply", [
    faux.reply('{"safe":false,"reason":"deletes external data"}'),
    faux.reply('{"safe":"true","reason":"not a boolean"}'),
    faux.reply('{"safe":true,"reason":""}'),
    faux.reply('{"safe":true,"reason":"x","risk":true}'),
    faux.reply("I approve"), faux.error("network unavailable"),
])
def test_risky_or_failed_review_requests_human_approval(make_session, tmp_path, reply):
    ui = RecordingUI(answers=["n"])
    s = make_session(ui=ui, mode="auto-review")
    s.small_model = s.resolve("faux/reviewer")
    faux.script("reviewer", reply)
    ev = event(s, "write", {"path": str(tmp_path / "outside.txt"), "content": "no"})
    block = s.services["permissions"].on_tool_call(ev, s)
    assert isinstance(block, Block) and "denied" in block.reason
    assert len(ui.questions) == 1
    assert not (tmp_path / "outside.txt").exists()


def test_risky_review_respects_allow_once_and_exact_session_approval(make_session, tmp_path, monkeypatch):
    reviewed = []
    def review(*args):
        reviewed.append(args)
        return False, "external write"
    monkeypatch.setattr(permission, "review_tool_call", review)
    ui = RecordingUI(answers=["y", "a", "n"])
    s = make_session(ui=ui, mode="auto-review")
    perms = s.services["permissions"]
    args = {"path": str(tmp_path / "outside.txt"), "content": "one"}
    assert perms.on_tool_call(event(s, "write", args), s) is None
    assert perms.on_tool_call(event(s, "write", args), s) is None
    assert perms.on_tool_call(event(s, "write", args), s) is None
    assert len(ui.questions) == len(reviewed) == 2
    assert isinstance(perms.on_tool_call(event(s, "write", args | {"content": "two"}), s), Block)
    assert len(ui.questions) == 3


def test_accept_all_bypasses_review_and_all_permission_rules(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("accept all was reviewed"))
    s = make_session(mode="accept-all", settings={"permissions": {"deny": ["bash(*)"]}})
    for command in ("curl https://example.test", "sudo reboot", "rm -rf /"):
        assert s.services["permissions"].decide(event(s, "bash", {"command": command})) == ("allow", "")


def test_plan_mode_only_allows_reads_and_dominates_agent_override(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("plan was reviewed"))
    s = make_session(mode="plan", settings={"permissions": {"allow": ["write", "bash(*)"]}})
    perms = s.services["permissions"]
    perms.set_agent_mode("child", "accept-all")
    assert perms.decide(event(s, "read", {"path": "README.md"}))[0] == "allow"
    assert perms.decide(event(s, "bash", {"command": "git status"}))[0] == "allow"
    for ev in (
        event(s, "write", {"path": "a.txt", "content": "x"}, agent="child"),
        event(s, "bash", {"command": "touch x"}),
        event(s, "team_spawn", {"worktree": True}),
        event(s, "team_review", {"approve": True}),
        event(s, "workflow", {"name": "example"}),
        event(s, "schedule", {"action": "add", "prompt": "change files"}),
        event(s, "memory", {"action": "save"}),
    ):
        assert perms.decide(ev)[0] == "deny", ev.tool_name


def test_auto_review_preserves_explicit_allow_and_deny_rules(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("explicit rule was reviewed"))
    s = make_session(mode="auto-review", settings={"permissions": {"allow": ["bash(curl *)"], "deny": ["write(*)"]}})
    assert s.services["permissions"].decide(event(s, "bash", {"command": "curl https://example.test"}))[0] == "allow"
    assert s.services["permissions"].decide(event(s, "write", {"path": "x", "content": ""}))[0] == "deny"


def test_unattended_review_fallback_is_denied(make_session, tmp_path, monkeypatch):
    class Headless(RecordingUI):
        def confirm(self, q, agent="main"):
            return None
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: (False, "external file"))
    s = make_session(ui=Headless(), mode="auto-review")
    block = s.services["permissions"].on_tool_call(event(s, "write", {"path": str(tmp_path / "x"), "content": ""}), s)
    assert isinstance(block, Block) and "nobody is available" in block.reason


def test_review_and_human_approval_recheck_changed_mode(make_session, tmp_path, monkeypatch):
    s = make_session(mode="auto-review")
    perms = s.services["permissions"]
    ev = event(s, "write", {"path": str(tmp_path / "outside"), "content": "x"})
    def review(*_):
        s.permission_mode = "plan"
        return True, "approved before mode switched"
    monkeypatch.setattr(permission, "review_tool_call", review)
    assert isinstance(perms.on_tool_call(ev, s), Block)
    s.permission_mode = "auto-review"
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: (False, "external"))
    def confirm(*_):
        s.permission_mode = "plan"
        return "y"
    monkeypatch.setattr(s.ui, "confirm", confirm)
    assert isinstance(perms.on_tool_call(ev, s), Block)


def test_execution_rechecks_mode_arguments_and_symlink_boundary(make_session, tmp_path, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: (False, "external file"))
    s = make_session(mode="auto-review")
    perms = s.services["permissions"]
    ev = event(s, "write", {"path": "new/file", "content": "x"})
    assert perms.on_tool_call(ev, s) is None
    assert perms.before_execute(ev) is None
    s.permission_mode = "plan"
    assert isinstance(perms.before_execute(ev), Block)
    s.permission_mode = "auto-review"
    assert perms.on_tool_call(ev, s) is None
    (s.cwd / "new").symlink_to(tmp_path, target_is_directory=True)
    assert isinstance(perms.before_execute(ev), Block)
    (s.cwd / "new").unlink()
    assert perms.on_tool_call(ev, s) is None
    ev.args["path"] = str(tmp_path / "external")
    assert isinstance(perms.before_execute(ev), Block)


def test_search_review_matches_actual_ignored_and_filtered_files(make_session, tmp_path, monkeypatch):
    reviews = []
    monkeypatch.setattr(permission, "review_tool_call", lambda *args: (reviews.append(args) or (False, "external link")))
    s = make_session(mode="auto-review")
    hidden = s.cwd / ".venv"
    hidden.mkdir()
    (hidden / "python").symlink_to(tmp_path / "outside")
    (s.cwd / "secret.txt").symlink_to(tmp_path / "outside")
    perms = s.services["permissions"]
    assert perms.decide(event(s, "grep", {"pattern": "value", "glob": "*.py"}))[0] == "allow"
    assert perms.decide(event(s, "glob", {"pattern": "**/*.py"}))[0] == "allow"
    assert not reviews
    assert perms.decide(event(s, "grep", {"pattern": "value"}))[0] == "ask"
    assert str(tmp_path / "outside") in reviews[0][2]


def test_review_uses_call_signal_instead_of_interrupted_main(make_session, tmp_path):
    s = make_session(mode="auto-review")
    s.small_model = s.resolve("faux/reviewer")
    s.main.agent.signal.set()
    faux.script("reviewer", faux.reply('{"safe":true,"reason":"ordinary read"}', delay=0.01))
    ev = event(s, "read", {"path": str(tmp_path / "outside")}, agent="teammate")
    ev.signal = threading.Event()
    assert s.services["permissions"].decide(ev)[0] == "allow"


@pytest.mark.parametrize("command", ["cat fixed link", "cd sub && cat ../fixed link", "git -C sub diff ../fixed link"])
def test_shell_session_approval_binds_all_resolved_path_targets(make_session, tmp_path, monkeypatch, command):
    reviews = []
    monkeypatch.setattr(permission, "review_tool_call", lambda *args: (reviews.append(args) or (False, "external read")))
    ui = RecordingUI(answers=["a", "n"])
    s = make_session(ui=ui, mode="auto-review")
    (s.cwd / "sub").mkdir()
    (s.cwd / "fixed").symlink_to(tmp_path / "fixed-outside")
    link = s.cwd / ("sub/link" if command.startswith(("cd", "git")) else "link")
    link.symlink_to(tmp_path / "first-outside")
    perms = s.services["permissions"]
    ev = event(s, "bash", {"command": command})
    assert perms.on_tool_call(ev, s) is None
    assert perms.on_tool_call(event(s, "bash", {"command": command}), s) is None
    assert len(reviews) == 1
    link.unlink()
    link.symlink_to(tmp_path / "second-outside")
    assert isinstance(perms.before_execute(ev), Block)
    assert len(reviews) == len(ui.questions) == 2


def test_dynamic_shell_approval_is_once_and_inherited_cdpath_needs_review(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: (False, "dynamic command"))
    ui = RecordingUI(answers=["a", "n"])
    s = make_session(ui=ui, mode="auto-review")
    perms = s.services["permissions"]
    ev = event(s, "bash", {"command": "cat $TARGET"})
    assert perms.on_tool_call(ev, s) is None
    assert not perms.session_approvals
    assert isinstance(perms.on_tool_call(event(s, "bash", ev.args), s), Block)
    monkeypatch.setenv("CDPATH", "sub")
    assert command_review_reason("cd name && cat file", s.cwd, s.cwd)


def test_rule_matching_paths(project):
    ev = ToolCallEvent("1", "edit", {"path": "src/app/main.py"}, None, "main", project)
    assert rule_matches("edit(src/**)", ev)
    assert rule_matches("edit", ev)
    assert not rule_matches("write(src/**)", ev)
    assert rule_matches("mcp__*", ToolCallEvent("1", "mcp__gh__issue", {}, None, "main", project))
