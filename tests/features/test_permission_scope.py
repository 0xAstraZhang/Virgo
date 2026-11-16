"""Resolved path targets bind an approval to the exact operation it approved."""
from __future__ import annotations

from virgo.harness.permission_scope import ApprovalBoundary, command_path_targets


def test_static_paths_are_resolved_against_the_command_directory(project):
    assert command_path_targets("cat a.txt", project) == (str(project / "a.txt"),)
    assert command_path_targets("cat a.txt sub/b.txt", project) == (
        str(project / "a.txt"),
        str(project / "sub/b.txt"),
    )


def test_dynamic_paths_cannot_be_remembered(project, monkeypatch):
    assert command_path_targets("cat $TARGET", project) is None
    assert command_path_targets("cat *.txt", project) is None
    assert command_path_targets("cat $(whoami)", project) is None
    monkeypatch.setenv("CDPATH", "sub")
    assert command_path_targets("cd name && cat file", project) is None


def test_path_targets_change_when_a_symlinked_target_changes(project, tmp_path):
    link = project / "link"
    link.symlink_to(tmp_path / "first")
    first = command_path_targets("cat link", project)
    link.unlink()
    link.symlink_to(tmp_path / "second")
    second = command_path_targets("cat link", project)
    assert first != second


def test_approval_boundary_identity_is_not_display_text():
    boundary = ApprovalBoundary("features.permission.scope.unknown_tool")
    assert boundary.key == "features.permission.scope.unknown_tool"
    assert boundary.describe()
