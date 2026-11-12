"""Permission modes and automatic review decide which tool calls run, ask, or stop."""

from __future__ import annotations

import json
import threading

import pytest

from virgo.agent.types import Block, ToolCallEvent
from virgo.ai.providers import faux
from virgo.ai.types import UserMessage
from virgo.features import permission
from virgo.features.permission import (
    MODES,
    ReviewDecision,
    normalize_permission_mode,
    permission_mode_label,
    rule_matches,
)
from virgo.features.permission_scope import command_review_reason
from virgo.harness.session import Runtime

from tests.support.harness import RecordingUI, tool_call_event, tool_results


def test_public_modes_and_legacy_config_aliases():
    assert MODES == ("auto-review", "accept-all", "plan")
    for old, new in (("default", "auto-review"), ("accept-edits", "auto-review"), ("yolo", "accept-all")):
        assert normalize_permission_mode(old) == new
    assert [permission_mode_label(mode) for mode in MODES] == ["auto review", "accept all", "plan"]
    with pytest.raises(ValueError):
        normalize_permission_mode("unknown")


def test_auto_review_executes_internal_changes_without_asking(make_session, project, monkeypatch):
    # The static workspace classifier is for POSIX syntax; PowerShell requires review.
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("internal action was reviewed"))
    ui = RecordingUI()
    s = make_session(
        faux.reply(tools=[("write", {"path": "a.txt", "content": "1"}), ("bash", {"command": "touch b.txt"})]),
        faux.reply("done"), ui=ui, mode="auto-review", settings={"shell": {"executable": "bash"}},
    )
    s.prompt("create the files")
    assert (project / "a.txt").read_text() == "1"
    assert (project / "b.txt").exists()
    assert not ui.questions and all(not result.is_error for result in tool_results(s))


def test_small_model_safe_verdict_allows_external_read_without_tools(make_session, tmp_path):
    s = make_session(mode="auto-review")
    s.small_model = s.resolve("faux/reviewer")
    faux.script("reviewer", faux.reply('{"decision":"allow","risk":"low","authorization":"implicit","reason":"ordinary public information","evidence_ids":[]}'))
    ev = tool_call_event(s, "read", {"path": str(tmp_path / "outside.txt")})
    assert s.services["permissions"].decide(ev) == ("allow", "")
    call = faux.CALLS["reviewer"][0]
    assert call["tools"] == []
    payload = json.loads(call["messages"][0]["content"])
    assert payload["action"]["tool"] == "read" and payload["action"]["arguments"] == ev.args
    assert "untrusted data" in call["system"]


@pytest.mark.parametrize("reply", [
    faux.reply('{"decision":"ask","risk":"high","authorization":"unknown","reason":"deletes external data","evidence_ids":[]}'),
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
    ev = tool_call_event(s, "write", {"path": str(tmp_path / "outside.txt"), "content": "no"})
    block = s.services["permissions"].on_tool_call(ev, s)
    assert isinstance(block, Block) and "denied" in block.reason
    assert len(ui.questions) == 1
    assert not (tmp_path / "outside.txt").exists()


def test_risky_review_respects_allow_once_and_exact_session_approval(make_session, tmp_path, monkeypatch):
    reviewed = []
    def review(*args):
        reviewed.append(args)
        return ReviewDecision("ask", "external write")
    monkeypatch.setattr(permission, "review_tool_call", review)
    ui = RecordingUI(answers=["y", "a", "n"])
    s = make_session(ui=ui, mode="auto-review")
    perms = s.services["permissions"]
    args = {"path": str(tmp_path / "outside.txt"), "content": "one"}
    assert perms.on_tool_call(tool_call_event(s, "write", args), s) is None
    assert perms.on_tool_call(tool_call_event(s, "write", args), s) is None
    assert perms.on_tool_call(tool_call_event(s, "write", args), s) is None
    assert len(ui.questions) == len(reviewed) == 2
    assert isinstance(perms.on_tool_call(tool_call_event(s, "write", args | {"content": "two"}), s), Block)
    assert len(ui.questions) == 3


def test_accept_all_bypasses_review_and_all_permission_rules(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("accept all was reviewed"))
    s = make_session(mode="accept-all", settings={"permissions": {"deny": ["bash(*)"]}})
    for command in ("curl https://example.test", "sudo reboot", "rm -rf /"):
        assert s.services["permissions"].decide(tool_call_event(s, "bash", {"command": command})) == ("allow", "")


def test_plan_mode_only_allows_reads_and_dominates_agent_override(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("plan was reviewed"))
    s = make_session(mode="plan", settings={"permissions": {"allow": ["write", "bash(*)"]}})
    perms = s.services["permissions"]
    perms.set_agent_mode("child", "accept-all")
    assert perms.decide(tool_call_event(s, "read", {"path": "README.md"}))[0] == "allow"
    assert perms.decide(tool_call_event(s, "bash", {"command": "git status"}))[0] == "allow"
    for ev in (
        tool_call_event(s, "write", {"path": "a.txt", "content": "x"}, agent="child"),
        tool_call_event(s, "bash", {"command": "touch x"}),
        tool_call_event(s, "team_spawn", {"worktree": True}),
        tool_call_event(s, "team_review", {"approve": True}),
        tool_call_event(s, "workflow", {"name": "example"}),
        tool_call_event(s, "schedule", {"action": "add", "prompt": "change files"}),
        tool_call_event(s, "memory", {"action": "save"}),
    ):
        assert perms.decide(ev)[0] == "deny", ev.tool_name


def test_auto_review_preserves_explicit_allow_and_deny_rules(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: pytest.fail("explicit rule was reviewed"))
    s = make_session(mode="auto-review", settings={"permissions": {"allow": ["bash(curl *)"], "deny": ["write(*)"]}})
    assert s.services["permissions"].decide(tool_call_event(s, "bash", {"command": "curl https://example.test"}))[0] == "allow"
    assert s.services["permissions"].decide(tool_call_event(s, "write", {"path": "x", "content": ""}))[0] == "deny"


def test_unattended_review_fallback_is_denied(make_session, tmp_path, monkeypatch):
    class Headless(RecordingUI):
        def confirm(self, q, agent="main"):
            return None
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: ReviewDecision("ask", "external file"))
    s = make_session(ui=Headless(), mode="auto-review")
    block = s.services["permissions"].on_tool_call(tool_call_event(s, "write", {"path": str(tmp_path / "x"), "content": ""}), s)
    assert isinstance(block, Block) and "nobody is available" in block.reason


def test_review_and_human_approval_recheck_changed_mode(make_session, tmp_path, monkeypatch):
    s = make_session(mode="auto-review")
    perms = s.services["permissions"]
    ev = tool_call_event(s, "write", {"path": str(tmp_path / "outside"), "content": "x"})
    def review(*_):
        s.permission_mode = "plan"
        return ReviewDecision("allow", "approved before mode switched")
    monkeypatch.setattr(permission, "review_tool_call", review)
    assert isinstance(perms.on_tool_call(ev, s), Block)
    s.permission_mode = "auto-review"
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: ReviewDecision("ask", "external"))
    def confirm(*_):
        s.permission_mode = "plan"
        return "y"
    monkeypatch.setattr(s.ui, "confirm", confirm)
    assert isinstance(perms.on_tool_call(ev, s), Block)


def test_execution_rechecks_mode_arguments_and_symlink_boundary(make_session, tmp_path, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: ReviewDecision("ask", "external file"))
    s = make_session(mode="auto-review")
    perms = s.services["permissions"]
    ev = tool_call_event(s, "write", {"path": "new/file", "content": "x"})
    assert perms.on_tool_call(ev, s) is None
    assert perms.before_execute(ev) is None
    s.permission_mode = "plan"
    assert isinstance(perms.before_execute(ev), Block)
    s.permission_mode = "auto-review"
    assert perms.on_tool_call(ev, s) is None
    (s.cwd / "new").symlink_to(tmp_path, target_is_directory=True)
    assert isinstance(perms.before_execute(ev), Block)
    (s.cwd / "new").unlink()
    s.main.messages.append(UserMessage("The external link is removed; proceed within the workspace."))
    assert perms.on_tool_call(ev, s) is None
    ev.args["path"] = str(tmp_path / "external")
    assert isinstance(perms.before_execute(ev), Block)


def test_search_review_matches_actual_ignored_and_filtered_files(make_session, tmp_path, monkeypatch):
    reviews = []
    monkeypatch.setattr(permission, "review_tool_call", lambda *args: (reviews.append(args) or ReviewDecision("ask", "external link")))
    s = make_session(mode="auto-review")
    hidden = s.cwd / ".venv"
    hidden.mkdir()
    (hidden / "python").symlink_to(tmp_path / "outside")
    (s.cwd / "secret.txt").symlink_to(tmp_path / "outside")
    perms = s.services["permissions"]
    assert perms.decide(tool_call_event(s, "grep", {"pattern": "value", "glob": "*.py"}))[0] == "allow"
    assert perms.decide(tool_call_event(s, "glob", {"pattern": "**/*.py"}))[0] == "allow"
    assert not reviews
    assert perms.decide(tool_call_event(s, "grep", {"pattern": "value"}))[0] == "ask"
    assert str(tmp_path / "outside") in reviews[0][2]


def test_search_review_selects_the_same_files_as_the_glob_tools(make_session, tmp_path, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *args: ReviewDecision("ask", "external link"))
    s = make_session(mode="auto-review")
    (s.cwd / "src" / "pkg").mkdir(parents=True)
    (s.cwd / "src" / "link.py").symlink_to(tmp_path / "outside")
    perms = s.services["permissions"]
    # `src/**/*.py` reaches files directly under src/, so the escaping link there must be reviewed.
    assert perms.decide(tool_call_event(s, "glob", {"pattern": "src/**/*.py"}))[0] == "ask"
    assert perms.decide(tool_call_event(s, "grep", {"pattern": "value", "glob": "src/**/*.py"}))[0] == "ask"
    # `src/pkg/*.py` and `src/*/*.py` never touch it.
    assert perms.decide(tool_call_event(s, "glob", {"pattern": "src/pkg/*.py"}))[0] == "allow"
    assert perms.decide(tool_call_event(s, "grep", {"pattern": "value", "glob": "src/*/*.py"}))[0] == "allow"


def test_review_uses_call_signal_instead_of_interrupted_main(make_session, tmp_path):
    s = make_session(mode="auto-review")
    s.small_model = s.resolve("faux/reviewer")
    s.main.agent.signal.set()
    faux.script("reviewer", faux.reply('{"decision":"allow","risk":"low","authorization":"implicit","reason":"ordinary read","evidence_ids":[]}', delay=0.01))
    ev = tool_call_event(s, "read", {"path": str(tmp_path / "outside")}, agent="teammate")
    runtime = Runtime(s, "teammate", "teammate")
    ev.review_source = runtime.review_source
    ev.signal = threading.Event()
    assert s.services["permissions"].decide(ev)[0] == "allow"


@pytest.mark.parametrize("command", ["cat fixed link", "cd sub && cat ../fixed link", "git -C sub diff ../fixed link"])
def test_shell_session_approval_binds_all_resolved_path_targets(make_session, tmp_path, monkeypatch, command):
    reviews = []
    monkeypatch.setattr(permission, "review_tool_call", lambda *args: (reviews.append(args) or ReviewDecision("ask", "external read")))
    ui = RecordingUI(answers=["a", "n"])
    s = make_session(ui=ui, mode="auto-review")
    (s.cwd / "sub").mkdir()
    (s.cwd / "fixed").symlink_to(tmp_path / "fixed-outside")
    link = s.cwd / ("sub/link" if command.startswith(("cd", "git")) else "link")
    link.symlink_to(tmp_path / "first-outside")
    perms = s.services["permissions"]
    ev = tool_call_event(s, "bash", {"command": command})
    assert perms.on_tool_call(ev, s) is None
    assert perms.on_tool_call(tool_call_event(s, "bash", {"command": command}), s) is None
    assert len(reviews) == 1
    link.unlink()
    link.symlink_to(tmp_path / "second-outside")
    assert isinstance(perms.before_execute(ev), Block)
    assert len(reviews) == len(ui.questions) == 2


def test_dynamic_shell_approval_is_once_and_inherited_cdpath_needs_review(make_session, monkeypatch):
    monkeypatch.setattr(permission, "review_tool_call", lambda *_: ReviewDecision("ask", "dynamic command"))
    ui = RecordingUI(answers=["a", "n"])
    s = make_session(ui=ui, mode="auto-review")
    perms = s.services["permissions"]
    ev = tool_call_event(s, "bash", {"command": "cat $TARGET"})
    assert perms.on_tool_call(ev, s) is None
    assert not perms.session_approvals
    assert isinstance(perms.on_tool_call(tool_call_event(s, "bash", ev.args), s), Block)
    monkeypatch.setenv("CDPATH", "sub")
    assert command_review_reason("cd name && cat file", s.cwd, s.cwd)


def test_rule_matching_paths(project):
    ev = ToolCallEvent("1", "edit", {"path": "src/app/main.py"}, None, "main", project)
    assert rule_matches("edit(src/**)", ev)
    assert rule_matches("edit", ev)
    assert not rule_matches("write(src/**)", ev)
    assert rule_matches("mcp__*", ToolCallEvent("1", "mcp__gh__issue", {}, None, "main", project))
