"""Independent code-review acceptance, specified before production implementation.

Failure modes: staged/unstaged/untracked or merge-base changes disappear; an
exact commit reads later files; deletion/rename anchors are misvalidated;
ordinary hooks, Git helpers, oversized results or model tools write files;
permission-denied contents leak through snapshot tools; incomplete model runs
become clean reports; cancellation leaks a child or poisons the next request;
saved conclusions silently survive code changes; /review hides a user template;
print/TUI clients lose reports or return success for an unsuccessful review.

These scenarios use real Git repositories, assembled sessions and the real
agent loop. Only model responses are scripted; no production Git, Runtime,
permission, persistence, command or tool implementation is replaced.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
from importlib import import_module

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.controller import RunController

from tests.support.harness import calls, wait_until
from tests.support.tui import pump


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        encoding="utf-8", check=True, timeout=15,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.stdout.strip()


def _write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _repository(root):
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Review Acceptance")
    _git(root, "config", "user.email", "review@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, "sample.py", "value = 1\n")
    _git(root, "add", "--", "sample.py")
    _git(root, "commit", "-m", "fixture: initial source")
    return _git(root, "rev-parse", "HEAD")


def _changed_repository(root):
    head = _repository(root)
    _write(root, "sample.py", "value = 0\n")
    return head


def _manifest(context):
    """Read the public snapshot JSON without depending on a prompt wrapper."""
    decoder = json.JSONDecoder()
    for message in context.messages:
        if not isinstance(message, UserMessage):
            continue
        text = message.text()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(value, dict):
                value = value.get("snapshot", value)
                if isinstance(value, dict) and "target" in value and isinstance(value.get("changes"), list):
                    return value
    raise AssertionError("The independent review did not receive its target snapshot manifest")


def _change(snapshot, path, layer=None):
    matches = [
        item for item in snapshot["changes"]
        if path in (item.get("old_path"), item.get("new_path"))
        and (layer is None or item["layer"] == layer)
    ]
    assert len(matches) == 1, (path, layer, snapshot["changes"])
    return matches[0]


def _finding(change, *, side="new", line=1, title="Handle the changed zero value"):
    return {
        "change_id": change["id"], "path": change[f"{side}_path"], "side": side,
        "start_line": line, "end_line": line, "priority": 2, "title": title,
        "explanation": "The changed value reaches an existing division and causes a zero-division failure.",
        "confidence": 0.95,
    }


def _answer(findings=(), *, status=None):
    data = {"summary": "Checked the selected changes.", "findings": list(findings)}
    if status is not None:
        data["status"] = status
    return faux.reply(json.dumps(data))


def _latest(session):
    rows = session.store.custom("code_review")
    assert rows, "The command did not save a code-review lifecycle/result"
    report = rows[-1]
    assert report["id"] and report["status"]
    return report


def _filesystem(root):
    """Include index/config bytes and new empty directories, not access times."""
    result = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = ("link", os.readlink(path))
        elif path.is_dir():
            result[name] = ("directory",)
        else:
            result[name] = ("file", path.read_bytes())
    return result


def test_review_uncommitted_layers_and_diff_sides_reach_a_saved_display(make_session, project):
    _repository(project)
    _write(project, "modified.py", "unchanged = 1\nvalue = 1\n")
    _write(project, "deleted.py", "check_authorization()\n")
    _write(project, "old name.py", "".join(f"unchanged_{i} = {i}\n" for i in range(30)) + "value = 1\n")
    _git(project, "add", "--", ".")
    _git(project, "commit", "-m", "fixture: tracked paths")
    _write(project, "modified.py", "unchanged = 1\nvalue = 0\n")
    _write(project, "added.py", "value = 0\n")
    _git(project, "mv", "--", "old name.py", "renamed name.py")
    _write(project, "renamed name.py", "".join(f"unchanged_{i} = {i}\n" for i in range(30)) + "value = 0\n")
    _git(project, "add", "--", ".")
    _write(project, "modified.py", "unchanged = 1\nvalue = -1\n")
    (project / "deleted.py").unlink()
    _write(project, "new file.py", "value = 0\n")
    session = make_session(faux.reply("Prior unrelated answer."), persist=True, refresh_models=False)
    session.prompt("PRIVATE_PARENT_CONVERSATION")
    parent_messages = list(session.main.messages)
    before = _filesystem(project)
    seen = {}

    def respond(context):
        assert "PRIVATE_PARENT_CONVERSATION" not in repr(context.messages)
        assert len(context.messages) == 1
        assert {tool.name for tool in context.tools} == {"read", "glob", "grep", "review_git"}
        snapshot = _manifest(context)
        seen.update(snapshot)
        assert snapshot["target"]["kind"] == "uncommitted"
        findings = [
            _finding(_change(snapshot, "modified.py", "staged"), line=2),
            _finding(_change(snapshot, "modified.py", "unstaged"), line=2),
            _finding(_change(snapshot, "added.py", "staged")),
            _finding(_change(snapshot, "new file.py", "untracked")),
            _finding(_change(snapshot, "deleted.py", "unstaged"), side="old"),
            _finding(_change(snapshot, "renamed name.py", "staged"), line=31),
        ]
        renamed = _change(snapshot, "renamed name.py", "staged")
        assert renamed["old_path"] == "old name.py" and renamed["new_path"] == "renamed name.py"
        return _answer(findings)

    faux.script("test", respond)
    session.prompt("/review")
    report = _latest(session)
    assert report["status"] == "completed" and len(report["findings"]) == 6
    assert report["snapshot"]["id"] == seen["id"]
    assert len([r for r in session.store.custom("code_review") if r["id"] == report["id"]]) >= 2
    assert session.main.messages == parent_messages
    assert "renamed name.py" in session.ui.text and "Handle the changed zero value" in session.ui.text
    assert "completed" in session.ui.text.lower()
    assert _filesystem(project) == before


def test_review_base_uses_merge_base_and_frozen_head_files(make_session, project):
    ancestor = _repository(project)
    _git(project, "checkout", "-b", "feature")
    _write(project, "sample.py", "FEATURE_HEAD = 0\n")
    _git(project, "add", "--", ".")
    _git(project, "commit", "-m", "fixture: feature change")
    feature = _git(project, "rev-parse", "HEAD")
    _git(project, "checkout", "main")
    _write(project, "base-only.py", "BASE_BRANCH_ONLY = True\n")
    _git(project, "add", "--", ".")
    _git(project, "commit", "-m", "fixture: baseline advance")
    baseline = _git(project, "rev-parse", "HEAD")
    _git(project, "checkout", "feature")
    _write(project, "sample.py", "DIRTY_WORKTREE = True\n")
    session = make_session(refresh_models=False)
    seen = {}

    def first(context):
        snapshot = _manifest(context)
        seen.update(snapshot)
        assert snapshot["merge_base"] == ancestor
        assert snapshot["head"] == feature and snapshot["base"] == baseline
        assert {item["new_path"] for item in snapshot["changes"]} == {"sample.py"}
        return faux.reply(tools=[("read", {"path": "sample.py", "version": "head"})])

    def last(context):
        result = next(message for message in reversed(context.messages) if isinstance(message, ToolResultMessage))
        assert not result.is_error and "FEATURE_HEAD" in result.text()
        assert "DIRTY_WORKTREE" not in result.text()
        return _answer([_finding(_change(seen, "sample.py", "base"))])

    faux.script("test", first, last)
    session.prompt("/review base main")
    assert _latest(session)["status"] == "completed"


@pytest.mark.parametrize("root_commit", [False, True], ids=["selected-commit", "root-commit"])
def test_review_commit_reads_the_selected_tree_not_head_or_worktree(make_session, project, root_commit):
    initial = _repository(project)
    if root_commit:
        target, expected = initial, "value = 1"
    else:
        _write(project, "sample.py", "SELECTED_COMMIT = 0\n")
        _git(project, "add", "--", ".")
        _git(project, "commit", "-m", "fixture: reviewed change")
        target, expected = _git(project, "rev-parse", "HEAD"), "SELECTED_COMMIT"
    _write(project, "sample.py", "LATER_COMMIT = True\n")
    _git(project, "add", "--", ".")
    _git(project, "commit", "-m", "fixture: later unrelated change")
    _write(project, "sample.py", "DIRTY_WORKTREE = True\n")
    session = make_session(refresh_models=False)
    seen = {}

    def first(context):
        seen.update(_manifest(context))
        assert seen["target"]["kind"] == "commit"
        return faux.reply(tools=[("read", {"path": "sample.py", "version": "target"})])

    def last(context):
        result = next(message for message in reversed(context.messages) if isinstance(message, ToolResultMessage))
        assert not result.is_error and expected in result.text()
        assert "LATER_COMMIT" not in result.text() and "DIRTY_WORKTREE" not in result.text()
        return _answer([_finding(_change(seen, "sample.py", "commit"))])

    faux.script("test", first, last)
    session.prompt(f"/review commit {target}")
    assert _latest(session)["status"] == "completed"


def test_review_tools_hooks_git_helpers_and_large_outputs_never_write(
    make_session, project, tmp_path, monkeypatch, python_command,
):
    _changed_repository(project)
    _write(project, "filtered.txt", "FILTER_BEFORE\n")
    _git(project, "add", "--", "filtered.txt")
    _git(project, "commit", "-m", "fixture: tracked filter candidate")
    _write(project, "filtered.txt", "FILTER_AFTER\n")
    _write(project, "large.txt", "".join(f"ROW_{i:04d} " + "x" * 70 + "\n" for i in range(1800)))
    _write(project, ".gitattributes", "*.txt diff=review_probe filter=review_probe\n")
    marker = project / "hook-executed.txt"
    hook = python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')")
    settings = {
        "permissions": {"allow": ["*"]},
        "hooks": {name: [{"matcher": ".*", "command": hook}] for name in (
            "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
        )},
    }
    session = make_session(settings=settings, mode="accept-all", refresh_models=False)
    _git(project, "config", "diff.external", hook)
    _git(project, "config", "diff.review_probe.textconv", hook)
    _git(project, "config", "filter.review_probe.clean", hook)
    _git(project, "config", "filter.review_probe.smudge", hook)
    _git(project, "config", "filter.review_probe.required", "true")
    _git(project, "config", "core.fsmonitor", hook)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", hook)
    before = _filesystem(project)
    outsider = tmp_path / "outside.txt"
    outsider.write_text("OUTSIDE_WORKSPACE_SECRET", encoding="utf-8")
    seen = []

    def first(context):
        assert {tool.name for tool in context.tools} == {"read", "glob", "grep", "review_git"}
        assert not context.web_tools and context.web_search is None
        _manifest(context)
        seen.append(True)
        return faux.reply(tools=[
            ("write", {"path": "owned.txt", "content": "bad"}),
            ("edit", {"path": "sample.py", "old_text": "0", "new_text": "1"}),
            ("bash", {"command": hook}),
            ("powershell", {"command": hook}),
            ("agent", {"description": "escape", "prompt": "write owned.txt"}),
            ("web_fetch", {"url": "https://example.invalid/"}),
            ("read", {"path": str(outsider)}),
            ("review_git", {"operation": "commit", "message": "escape"}),
            ("read", {"path": "large.txt", "limit": 2000}),
            ("review_git", {"operation": "diff", "limit": 2000}),
        ])

    def last(context):
        results = [message for message in context.messages if isinstance(message, ToolResultMessage)]
        assert len(results) == 10
        assert all(message.is_error for message in results[:8])
        assert "OUTSIDE_WORKSPACE_SECRET" not in repr(context.messages)
        assert all(len(message.text().encode("utf-8")) < 60 * 1024 for message in results)
        return _answer()

    faux.script("test", first, last)
    session.prompt("/review")
    assert seen and _latest(session)["status"] in {"completed", "incomplete"}
    assert _filesystem(project) == before
    assert not marker.exists() and not (project / ".virgo" / "outputs").exists()


def test_review_rejects_malformed_schema_and_unrelated_diff_locations(make_session, project):
    _repository(project)
    _write(project, "sample.py", "unchanged = 1\nvalue = 1\n")
    _git(project, "add", "--", ".")
    _git(project, "commit", "-m", "fixture: line anchors")
    _write(project, "sample.py", "unchanged = 1\nvalue = 0\n")
    _write(project, "added.py", "value = 0\n")
    session = make_session(refresh_models=False)
    variants = ["json", "schema", "line", "unchanged", "path", "side", "priority", "confidence"]
    for variant in variants:
        def invalid(context, variant=variant):
            if variant == "json":
                return faux.reply("Here is the report, without JSON.")
            if variant == "schema":
                return faux.reply('{"findings": []}')
            snapshot = _manifest(context)
            finding = _finding(_change(snapshot, "sample.py"), line=2)
            if variant == "line":
                finding["start_line"] = finding["end_line"] = 999
            elif variant == "unchanged":
                finding["start_line"] = finding["end_line"] = 1
            elif variant == "path":
                finding["path"] = "../outside.py"
            elif variant == "side":
                finding = _finding(_change(snapshot, "added.py"))
                finding["side"] = "old"
            elif variant == "priority":
                finding["priority"] = True
            elif variant == "confidence":
                finding["confidence"] = float("nan")
            return _answer([finding])

        faux.script("test", invalid)
        session.prompt("/review")
        report = _latest(session)
        assert report["status"] == "invalid_report", (variant, report)
        assert report.get("error"), (variant, report)
        assert not report["findings"]
    assert not session._child_runtimes


@pytest.mark.parametrize("ending, expected", [
    ("empty", {"invalid_report", "incomplete"}),
    ("truncated", {"incomplete"}),
    ("refusal", {"failed", "incomplete"}),
    ("provider", {"failed"}),
    ("overflow", {"incomplete"}),
    ("insufficient", {"incomplete"}),
])
def test_review_unsuccessful_model_runs_cannot_become_clean_results(make_session, project, ending, expected):
    _changed_repository(project)
    session = make_session(refresh_models=False, settings={"retry": {"maxRetries": 0}})
    replies = {
        "empty": faux.reply(""),
        "truncated": faux.reply(_answer().text, stop_reason="length"),
        "refusal": faux.reply("I cannot complete this review.", stop_reason="refusal"),
        "provider": faux.error("The model request failed."),
        "overflow": faux.error("Context window exceeded.", kind="overflow"),
        "insufficient": _answer(status="incomplete"),
    }
    faux.script("test", replies[ending])
    session.prompt("/review")
    report = _latest(session)
    assert report["status"] in expected, report
    assert not report["findings"]
    assert len(calls()) == 1
    assert not session._child_runtimes
    assert report["status"] in session.ui.text.lower()


def test_review_shared_budget_exhaustion_is_not_a_clean_report(make_session, project):
    _changed_repository(project)
    session = make_session(refresh_models=False, settings={"budget": {"maxModelCalls": 1}})
    faux.script("test", faux.reply(tools=[("read", {"path": "sample.py"})]), _answer())
    session.prompt("/review")
    report = _latest(session)
    assert report["status"] in {"failed", "incomplete"}
    assert len(calls()) == 1 and session.main.request_budget.used == 1
    assert "budget" in json.dumps(report).lower()
    assert not session._child_runtimes


def test_review_tui_cancellation_releases_runtime_and_the_next_review_works(make_session, project):
    _changed_repository(project)
    ui = FullscreenUI()
    session = make_session(ui=ui, refresh_models=False)
    controller = RunController(session, ui)
    baseline_signals = tuple(session.store.failure_signals)
    policy = session.services["permissions"]
    baseline_modes, baseline_parents = dict(policy.agent_modes), dict(policy.agent_parents)
    faux.script("test", faux.reply(_answer().text, delay=20))
    controller.start("/review")
    try:
        wait_until(lambda: bool(calls()) and bool(session._child_runtimes))
        child = next(iter(session._child_runtimes.values()))
        assert child.parent is session.main and child.cwd == project
        assert child.request_budget is session.main.request_budget
        controller.abort()
        updates = pump(controller)
        assert _latest(session)["status"] == "cancelled"
        assert not child.agent.running and not session._child_runtimes
        assert tuple(session.store.failure_signals) == baseline_signals
        assert policy.agent_modes == baseline_modes and policy.agent_parents == baseline_parents
        assert "cancelled" in repr(updates).lower()
        faux.script("test", _answer())
        controller.submit("/review")
        pump(controller)
        assert _latest(session)["status"] == "completed"
        assert not session._child_runtimes
    finally:
        controller.abort()
        if controller._worker is not None:
            controller._worker.join(3)


def test_review_saved_report_reopens_and_marks_changed_workspace_stale(make_session, project):
    _changed_repository(project)
    session = make_session(persist=True, refresh_models=False)
    faux.script("test", _answer())
    session.prompt("/review")
    original = copy.deepcopy(_latest(session))
    assert original["status"] == "completed"
    path = session.store.path
    session.shutdown()
    resumed = make_session(session=path, refresh_models=False)
    resumed.prompt(f"/review show {original['id']}")
    assert "current" in resumed.ui.text.lower()
    assert not calls(), "Displaying a stored review must not call the model"
    resumed.ui.lines.clear()
    _write(project, "sample.py", "value = 7\n")
    resumed.prompt("/review show last")
    assert "stale" in resumed.ui.text.lower()
    saved = [row for row in resumed.store.custom("code_review") if row["id"] == original["id"]]
    assert saved[-1]["snapshot"]["id"] == original["snapshot"]["id"]
    assert not calls()


def test_review_template_priority_and_optional_plugin_are_explicit(make_session, project):
    _changed_repository(project)
    _write(project, ".virgo/prompts/review.md", "MY_EXISTING_REVIEW_TEMPLATE $@")
    session = make_session(faux.reply("Template result."), refresh_models=False)
    assert session.commands.get("review") is None and "review" in session.templates
    assert "/review" in session.ui.text and "/code-review" in session.ui.text
    session.prompt("/review retained arguments")
    assert "MY_EXISTING_REVIEW_TEMPLATE retained arguments" in repr(calls())
    faux.script("test", _answer())
    session.prompt("/code-review")
    assert _latest(session)["status"] == "completed"
    minimal = make_session(settings={"profile": "minimal"}, refresh_models=False)
    assert "code_review" not in minimal.services and minimal.commands.get("code-review") is None
    enabled = make_session(
        settings={"profile": "minimal", "plugins": {"code_review": {"enabled": True}}},
        refresh_models=False,
    )
    faux.script("test", _answer())
    enabled.prompt("/code-review")
    assert _latest(enabled)["status"] == "completed"


def test_review_cli_json_contains_validated_report_and_failure_exit_status(project, capsys, monkeypatch):
    _changed_repository(project)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    cli = import_module("virgo.cli.main")
    argv = ["-C", str(project), "--no-session", "--no-extensions", "-m", "faux/review-cli",
            "-p", "--mode", "json", "/review"]

    def success(context):
        return _answer([_finding(_change(_manifest(context), "sample.py"))])

    faux.script("review-cli", success)
    assert cli.main(argv) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    report = next(event["report"] for event in events if event["type"] == "code_review")
    assert report["status"] == "completed" and report["freshness"] == "current"
    assert report["findings"][0]["path"] == "sample.py"
    faux.script("review-cli", faux.reply("not JSON"))
    assert cli.main(argv) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(event["report"] for event in events if event["type"] == "code_review")["status"] == "invalid_report"


def test_review_invalid_targets_and_denied_contents_do_not_become_empty_success(make_session, project):
    _changed_repository(project)
    session = make_session(refresh_models=False)
    before = _filesystem(project)
    for command in ("/review base --output=owned.txt", "/review commit 'HEAD; touch owned.txt'",
                    "/review commit unknown-revision"):
        session.prompt(command)
        assert _latest(session)["status"] == "failed"
        assert not calls()
    assert _filesystem(project) == before

    _write(project, "private.txt", "DENIED_CONTENT_MUST_NOT_REACH_THE_MODEL\n")
    denied = make_session(
        refresh_models=False, settings={"permissions": {"deny": ["read(private.txt)"]}},
    )
    faux.script("test", _answer())
    denied.prompt("/review")
    assert _latest(denied)["status"] in {"failed", "incomplete"}
    assert "DENIED_CONTENT_MUST_NOT_REACH_THE_MODEL" not in repr(calls())


def test_review_binary_context_is_incomplete_and_does_not_mutate_the_target(make_session, project):
    _changed_repository(project)
    (project / "opaque.bin").write_bytes(b"\x00\xff\x00\x01")
    session = make_session(refresh_models=False)
    before = _filesystem(project)
    faux.script("test", _answer())
    session.prompt("/review")
    assert _latest(session)["status"] == "incomplete"
    assert _filesystem(project) == before
