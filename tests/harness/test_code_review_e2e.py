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
import threading
import tracemalloc
from importlib import import_module

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage, UserMessage
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.controller import RunController
from virgo.tools.git_read import MAX_GIT_BYTES

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


@pytest.mark.parametrize("work_mode", ["default", "plan"])
def test_review_context_is_independent_of_planning_intent(make_session, project, work_mode):
    """A review must receive review intent, independently of planning instructions."""
    _changed_repository(project)
    seen = []

    def respond(context):
        seen.append(context.system)
        return _answer()

    session = make_session(respond, settings={"workMode": work_mode}, refresh_models=False)
    session.prompt("/review")

    assert _latest(session)["status"] == "completed"
    assert len(seen) == 1
    assert "independent code reviewer" in seen[0]
    assert "Plan mode is active" not in seen[0]
    assert "produce a decision-complete" not in seen[0]
    assert session.services["permissions"].work_mode == work_mode


def test_review_uses_permission_mode_selected_during_previous_run(make_session, project):
    """A new isolated task must not inherit the previous completed run's authority."""
    _changed_repository(project)

    def select_next_mode(_context):
        session.services["sandbox"].select_mode("read-only")
        return faux.reply("Finished the previous task.")

    session = make_session(
        select_next_mode,
        faux.reply(tools=[("write", {"path": "review-write.txt", "content": "unexpected"})]),
        _answer(), refresh_models=False,
    )
    manager = session.services["sandbox"]
    session.prompt("Finish the current task")
    assert manager.selected_mode_id == "read-only"
    assert manager.mode_for(session.main).id == "no-limit"

    session.prompt("/review")

    assert not (project / "review-write.txt").exists()
    assert manager.mode_for(session.main).read_only
    assert '"read_only": true' in calls()[-1]["system"]
    assert _latest(session)["status"] == "completed"
    assert _latest(session)["freshness"] == "current"


def test_review_commit_ignores_unrelated_oversized_worktree_context(make_session, project):
    _repository(project)
    _write(project, "large.txt", "unchanged\n" * 120_000)
    _git(project, "add", "--", "large.txt")
    _git(project, "commit", "-m", "fixture: unrelated large file")
    _write(project, "sample.py", "value = 0\n")
    _git(project, "add", "--", "sample.py")
    _git(project, "commit", "-m", "fixture: small selected change")

    session = make_session(_answer(), refresh_models=False)
    session.prompt("/review commit HEAD")

    report = _latest(session)
    assert report["status"] == "completed", report
    assert [item["new_path"] for item in report["snapshot"]["changes"]] == ["sample.py"]
    assert len(calls()) == 1

    # Laziness cannot turn missing evidence into a clean conclusion once the
    # reviewer requests that oversized worktree context explicitly.
    def after_read(context):
        result = next(message for message in reversed(context.messages) if isinstance(message, ToolResultMessage))
        assert result.is_error and "limit" in result.text().lower()
        return _answer()

    faux.script("test", faux.reply(tools=[("review_read", {"path": "large.txt", "version": "worktree"})]), after_read)
    session.prompt("/review commit HEAD")
    report = _latest(session)
    assert report["status"] == "incomplete", report
    assert "Missing context:" in report["summary"] and "large.txt" in report["summary"]


def test_review_file_replaced_by_directory_keeps_deletion_and_children(make_session, project):
    _repository(project)
    (project / "sample.py").unlink()
    _write(project, "sample.py/child.txt", "new child\n")
    before = _filesystem(project)

    session = make_session(_answer(), refresh_models=False)
    session.prompt("/review")

    report = _latest(session)
    assert report["status"] == "completed", report
    removed = _change(report["snapshot"], "sample.py", "unstaged")
    added = _change(report["snapshot"], "sample.py/child.txt", "untracked")
    assert removed["status"] == "D" and removed["new_path"] is None
    assert added["status"] == "A" and added["old_path"] is None
    assert _filesystem(project) == before

    # The reverse replacement also keeps the removed child and new parent file.
    reverse = project.parent / "reverse-replacement"
    reverse.mkdir()
    _repository(reverse)
    _write(reverse, "package/old.py", "old = True\n")
    _git(reverse, "add", "--", "package/old.py")
    _git(reverse, "commit", "-m", "fixture: tracked directory")
    (reverse / "package/old.py").unlink()
    (reverse / "package").rmdir()
    _write(reverse, "package", "replacement file\n")
    before = _filesystem(reverse)
    session = make_session(_answer(), cwd=reverse, refresh_models=False)
    session.prompt("/review")

    report = _latest(session)
    assert report["status"] == "completed", report
    removed = _change(report["snapshot"], "package/old.py", "unstaged")
    added = _change(report["snapshot"], "package", "untracked")
    assert removed["status"] == "D" and removed["new_path"] is None
    assert added["status"] == "A" and added["old_path"] is None
    assert _filesystem(reverse) == before


@pytest.mark.parametrize("output", ["stdout", "stderr", "combined"])
def test_plan_git_output_budget_stops_oversized_streams_and_releases_resources(make_session, project, monkeypatch, output):
    _repository(project)
    if output != "stderr":
        # Large committed text is produced by real Git, without a fake pipe or
        # an arbitrary command in the read-only tool's argv.
        with (project / "large.txt").open("wb") as stream:
            block = (b"x" * 1023 + b"\n") * 1024
            for _ in range(40 if output == "stdout" else 20):
                stream.write(block)
        _git(project, "add", "--", "large.txt")
        _git(project, "commit", "-m", "fixture: large committed output")
    if output != "stdout":
        # Git itself emits a warning per invalid attribute line. The combined
        # case keeps each stream below 32 MiB while exceeding their shared budget.
        _write(project, ".gitattributes", "* invalid!name\n" * (300_000 if output == "stderr" else 150_000))
    session = make_session(
        faux.reply(tools=[("git_read", {"operation": "show", "revision": "HEAD"})]),
        faux.reply("The selected evidence exceeds the bounded reader."),
        settings={"profile": "minimal", "workMode": "plan", "plugins": {"plan": {"enabled": True}}}, refresh_models=False,
    )
    started = []
    spawn = subprocess.Popen

    def observe_process(*args, **kwargs):
        process = spawn(*args, **kwargs)  # Observe lifecycle; execution and pipes stay real.
        started.append(process)
        return process

    threads_before = {thread.ident for thread in threading.enumerate()}
    with monkeypatch.context() as patch:
        patch.setattr("virgo.tools.git_read.subprocess.Popen", observe_process)
        tracemalloc.start()
        try:
            session.prompt("Read the committed patch.")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    result = next(message for message in session.main.messages if isinstance(message, ToolResultMessage))
    assert result.is_error and "read limit" in result.text(), result.text()[:500]
    assert peak < MAX_GIT_BYTES + 16 * 1024 * 1024, f"Git reader allocated {peak} bytes before rejecting output"
    shown = next(process for process in started if "show" in process.args)
    assert shown.returncode is not None, "Over-budget Git must be reaped"
    if output == "stdout":
        # This patch still has several MiB to write when the reader closes it.
        # A warning-only producer may already have exited when its cap is seen.
        assert shown.returncode != 0, "The oversized patch producer must stop early"
    assert all(process.returncode is not None for process in started)
    assert all(stream is None or stream.closed for process in started
               for stream in (process.stdin, process.stdout, process.stderr))
    assert {thread.ident for thread in threading.enumerate()} <= threads_before

    faux.script("test", faux.reply(tools=[("git_read", {"operation": "log"})]), faux.reply("History remains usable."))
    session.prompt("Read the bounded log after the rejected patch.")
    results = [message for message in session.main.messages if isinstance(message, ToolResultMessage)]
    assert not results[-1].is_error


def test_plan_git_reads_committed_objects_without_worktree_helpers(make_session, project, python_command):
    base = _repository(project)
    _write(project, "sample.py", "value = 0\n")
    _git(project, "add", "--", "sample.py")
    _git(project, "commit", "-m", "fixture: selected change")
    command = python_command(f"from pathlib import Path; Path({str(project / 'helper-ran')!r}).write_text('bad')")
    _git(project, "config", "filter.untrusted.clean", command)
    _git(project, "config", "diff.untrusted.textconv", command)
    _git(project, "config", "diff.external", command)
    _write(project, ".gitattributes", "* filter=untrusted diff=untrusted\n")
    _write(project, "sample.py", "uncommitted content must not be shown\n")
    before = _filesystem(project)
    session = make_session(
        faux.reply(tools=[("git_read", {"operation": "log"}),
                          ("git_read", {"operation": "show", "revision": "HEAD"}),
                          ("git_read", {"operation": "diff", "base": base, "revision": "HEAD"})]),
        faux.reply("Investigated committed history."),
        settings={"profile": "minimal", "workMode": "plan", "plugins": {"plan": {"enabled": True}}}, refresh_models=False,
    )

    session.prompt("Inspect committed history without running project commands.")

    results = [message for message in session.main.messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 3 and all(not result.is_error for result in results)
    assert "selected change" in results[0].text()
    assert "+value = 0" in results[1].text() and "+value = 0" in results[2].text()
    assert "uncommitted content" not in repr(results)
    assert _filesystem(project) == before


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
        assert {"read", "write", "edit", "review_read", "review_glob", "review_grep", "review_git"}.issubset(
            tool.name for tool in context.tools
        )
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


@pytest.mark.parametrize("symlinks", ["true", "false"], ids=["type-change", "plain-checkout"])
def test_review_symlink_type_is_independent_of_executable_bit_tracking(make_session, project, symlinks):
    _repository(project)
    _write(project, "link.txt", "sample.py")
    _git(project, "add", "--", "link.txt")
    blob = _git(project, "rev-parse", ":link.txt")
    # Store a symlink without requiring the host to permit creating one.
    _git(project, "update-index", "--cacheinfo", f"120000,{blob},link.txt")
    _git(project, "commit", "-m", "fixture: tracked symlink")
    _git(project, "config", "core.filemode", "false")
    _git(project, "config", "core.symlinks", symlinks)
    assert _git(project, "diff", "--name-status") == ("T\tlink.txt" if symlinks == "true" else "")
    session = make_session(_answer(), refresh_models=False)

    session.prompt("/review")

    report = _latest(session)
    assert report["status"] == "completed" and report["freshness"] == "current"
    if symlinks == "true":
        change = _change(report["snapshot"], "link.txt", "unstaged")
        assert (change["old_mode"], change["new_mode"]) == ("120000", "100644")
        assert len(calls()) == 1
    else:
        assert report["snapshot"]["changes"] == []
        assert not calls()


@pytest.mark.parametrize("missing_side", ["new", "old"], ids=["remove-final-lf", "add-final-lf"])
def test_review_frozen_evidence_distinguishes_final_newlines(make_session, project, missing_side):
    _repository(project)
    before = "first\njob\n" if missing_side == "new" else "first\njob"
    after = "first\njob" if missing_side == "new" else "first\njob\n"
    _write(project, "data.txt", before)
    _git(project, "add", "--", "data.txt")
    _git(project, "commit", "-m", "fixture: newline-delimited records")
    _write(project, "data.txt", after)
    marker = "\\ No newline at end of file"
    assert marker in _git(project, "diff", "--", "data.txt")
    session = make_session(refresh_models=False)
    seen = {}

    def first(context):
        change = _change(_manifest(context), "data.txt", "unstaged")
        seen["change"] = change
        return faux.reply(tools=[
            ("review_git", {"operation": "diff", "change_id": change["id"]}),
            ("review_read", {"path": "data.txt", "version": "index"}),
            ("review_read", {"path": "data.txt", "version": "worktree"}),
        ])

    def last(context):
        results = [message for message in context.messages if isinstance(message, ToolResultMessage)]
        assert len(results) == 3 and not any(result.is_error for result in results)
        seen["evidence"] = [json.loads(result.text()) for result in results]
        return _answer([_finding(seen["change"], line=2, title="Check the final record's line terminator")])

    faux.script("test", first, last)
    session.prompt("/review")

    report = _latest(session)
    assert report["status"] == "completed", report
    diff, old, new = seen["evidence"]
    missing_line = ("+" if missing_side == "new" else "-") + "job"
    start = diff["lines"].index(missing_line)
    assert diff["lines"][start:start + 2] == [missing_line, marker]
    assert diff["lines"].count(marker) == 1
    assert old["lines"] == new["lines"] == ["first", "job"]
    assert old["ends_with_newline"] is before.endswith("\n")
    assert new["ends_with_newline"] is after.endswith("\n")
    assert report["findings"][0]["start_line"] == 2


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
        return faux.reply(tools=[("review_read", {"path": "sample.py", "version": "head"})])

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
        return faux.reply(tools=[("review_read", {"path": "sample.py", "version": "target"})])

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
    marker = tmp_path / "hook-executed.txt"
    helper_marker = project / "git-helper-executed.txt"
    hook = python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')")
    helper = python_command(f"from pathlib import Path; Path({str(helper_marker)!r}).write_text('executed')")
    settings = {
        "permissions": {"allow": ["*"]},
        "hooks": {name: [{"matcher": ".*", "command": hook}] for name in ("PreToolUse", "PostToolUse")},
    }
    session = make_session(settings=settings, approval="never", refresh_models=False)
    for key in ("diff.external", "diff.review_probe.textconv", "filter.review_probe.clean",
                "filter.review_probe.smudge", "core.fsmonitor"):
        _git(project, "config", key, helper)
    _git(project, "config", "filter.review_probe.required", "true")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", helper)
    outsider = tmp_path / "outside.txt"
    outsider.write_text("OUTSIDE_WORKSPACE_SECRET", encoding="utf-8")
    seen = []

    def first(context):
        assert {"read", "write", "edit", "review_read", "review_glob", "review_grep", "review_git"}.issubset(
            tool.name for tool in context.tools
        )
        _manifest(context)
        seen.append(True)
        return faux.reply(tools=[
            ("write", {"path": "owned.txt", "content": "allowed by current permissions"}),
            ("review_read", {"path": str(outsider)}),
            ("review_git", {"operation": "commit", "message": "invalid snapshot operation"}),
            ("review_read", {"path": "large.txt", "limit": 2000}),
            ("review_git", {"operation": "diff", "limit": 2000}),
        ])

    def last(context):
        results = [message for message in context.messages if isinstance(message, ToolResultMessage)]
        assert len(results) == 5
        assert not results[0].is_error  # Deliberate prompt noncompliance is not a sandbox denial.
        assert results[1].is_error and results[2].is_error
        assert "OUTSIDE_WORKSPACE_SECRET" not in repr(context.messages)
        assert all(len(message.text().encode("utf-8")) < 60 * 1024 for message in results)
        return _answer()

    faux.script("test", first, last)
    session.prompt("/review")
    report = _latest(session)
    assert seen and report["status"] in {"completed", "incomplete"}
    assert report["freshness"] == "stale"
    assert (project / "owned.txt").read_text() == "allowed by current permissions"
    assert marker.exists() and not helper_marker.exists()


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
    session = make_session(refresh_models=False, settings={
        "retry": {"maxRetries": 0}, "compaction": {"enabled": ending != "overflow"},
    })
    replies = {
        "empty": faux.reply(""),
        "truncated": faux.reply(_answer().text, stop_reason="length"),
        "refusal": faux.reply("I cannot complete this review.", stop_reason="refusal"),
        "provider": faux.error("The model request failed."),
        "overflow": faux.error("Context window exceeded.", kind="overflow"),
        "insufficient": _answer(status="incomplete"),
    }
    faux.script("test", replies[ending])
    if ending == "overflow":
        faux.handler("test", lambda context: (
            faux.reply("The frozen target still requires review.")
            if "context summarization assistant" in context.system else replies[ending]
        ))
    session.prompt("/review")
    report = _latest(session)
    assert report["status"] in expected, report
    assert not report["findings"]
    assert len(calls()) == (3 if ending == "overflow" else 1)
    if ending == "overflow":
        assert "independent code reviewer" in calls()[-1]["system"]
        assert {"write", "review_read", "review_git"}.issubset(calls()[-1]["tools"])
    assert not session._child_runtimes
    assert report["status"] in session.ui.text.lower()


def test_review_shared_budget_exhaustion_is_not_a_clean_report(make_session, project):
    _changed_repository(project)
    session = make_session(refresh_models=False, settings={"budget": {"maxModelCalls": 1}})
    faux.script("test", faux.reply(tools=[("review_read", {"path": "sample.py"})]), _answer())
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
    closed = []
    session.bus.on("runtime_closed", closed.append)
    faux.script("test", faux.reply(_answer().text, delay=20))
    controller.start("/review")
    try:
        wait_until(lambda: bool(calls()) and bool(session._child_runtimes))
        child = next(iter(session._child_runtimes.values()))
        assert child.parent is session.main and child.cwd == project
        assert child.request_budget is session.main.request_budget
        assert ui._signals[child.name] is child.agent.signal
        controller.abort()
        updates = pump(controller)
        assert _latest(session)["status"] == "cancelled"
        assert not child.agent.running and not session._child_runtimes
        assert tuple(session.store.failure_signals) == baseline_signals
        assert ui._signals == {"main": session.main.agent.signal}
        assert closed == [child]
        session.release_runtime(child)
        assert closed == [child]
        assert not child.read_only
        assert child.workflow == "review"
        assert "cancelled" in repr(updates).lower()
        faux.script("test", _answer())
        controller.submit("/review")
        pump(controller)
        assert _latest(session)["status"] == "completed"
        assert not session._child_runtimes
        assert ui._signals == {"main": session.main.agent.signal}
        for _ in range(2):
            controller.submit("/review show")
            pump(controller)
            assert not session._child_runtimes
            assert ui._signals == {"main": session.main.agent.signal}
        assert len(closed) == 4
    finally:
        controller.abort()
        if controller._worker is not None:
            controller._worker.join(3)


def test_review_tui_capture_never_waits_for_new_approval(make_session, project):
    _changed_repository(project)
    outside = project.parent / "outside-review.txt"
    outside.write_text("EXTERNAL_CONTENT_IS_NOT_REVIEW_EVIDENCE\n", encoding="utf-8")
    (project / "outside-link.txt").symlink_to(outside)
    ui = FullscreenUI()
    session = make_session(ui=ui, refresh_models=False, approval="never", allow=False)
    controller = RunController(session, ui)
    baseline_signals = tuple(session.store.failure_signals)
    controller.start()
    try:
        for mode, allow, deny, expected in (
            ("workspace-edit", [], [], "incomplete"),
            ("no-limit", [], [], "completed"),
            ("no-limit", [], ["read(outside-link.txt)"], "incomplete"),
            ("workspace-edit", ["read(outside-link.txt)"], [], "incomplete"),
        ):
            session.services["sandbox"].select_mode(mode)
            session.settings.set("permissions.allow", allow)
            session.settings.set("permissions.deny", deny)
            faux.script("test", _answer())
            controller.submit("/review")
            updates = pump(controller, lambda c: not c.busy or ui.has_pending_requests)
            if ui.has_pending_requests:
                # The original bug waits before agent_start has bound a signal.
                # Interrupting the real TUI must not leave this waiter stranded.
                controller.abort()
                controller._worker.join(0.25)
            assert not ui.has_pending_requests, "Review capture stranded an approval waiter after abort"
            assert not any(update.kind == "confirmation" for update in updates)
            updates.extend(pump(controller))
            assert _latest(session)["status"] == expected
            assert len(calls()) == (1 if expected == "completed" else 0)
            assert not session._child_runtimes and not session.busy
            assert tuple(session.store.failure_signals) == baseline_signals
            assert session.approval_policy == "never"
            assert session.services["permissions"].approval_for(session.main) == "never"
            assert "EXTERNAL_CONTENT_IS_NOT_REVIEW_EVIDENCE" not in repr(calls())
        session.approval_policy = "ask"
        session.settings.set("permissions.allow", [])
        session.settings.set("permissions.deny", [])
        session.services["permissions"].revoke()
        (project / "outside-link.txt").unlink()
        faux.script("test", faux.reply(tools=[("request_permissions", {
            "tool": "read", "arguments": {"path": str(outside)},
            "permissions": {"workspace_only": False},
            "reason": "Inspect the explicit external context needed for review",
        })]), _answer())
        controller.submit("/review")
        pump(controller, lambda c: not c.busy or ui.has_pending_requests)
        assert ui.has_pending_requests
        controller.abort()
        pump(controller)
        assert not ui.has_pending_requests and not session._child_runtimes
        assert _latest(session)["status"] == "cancelled"
        assert len(calls()) == 1
    finally:
        controller.abort()
        ui.cancel_confirmations()
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
    with pytest.raises(ValueError, match="reserved command /review"):
        make_session(refresh_models=False)
    minimal = make_session(faux.reply("Template result."), settings={"profile": "minimal"}, refresh_models=False)
    assert "code_review" not in minimal.services and minimal.commands.get("review") is None
    minimal.prompt("/review retained arguments")
    assert "MY_EXISTING_REVIEW_TEMPLATE retained arguments" in repr(calls())
    (project / ".virgo/prompts/review.md").rename(project / ".virgo/prompts/custom-review.md")
    enabled = make_session(
        settings={"profile": "minimal", "plugins": {"code_review": {"enabled": True}}},
        refresh_models=False,
    )
    faux.script("test", _answer())
    enabled.prompt("/review")
    assert _latest(enabled)["status"] == "completed"
    assert enabled.commands.get("code-review") is None


def test_review_cli_json_contains_validated_report_and_failure_exit_status(project, capsys, monkeypatch):
    _changed_repository(project)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    cli = import_module("virgo.cli.main")
    argv = ["-C", str(project), "--no-session", "--no-extensions", "-m", "faux/review-cli",
            "-p", "--format", "json", "/review"]

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
