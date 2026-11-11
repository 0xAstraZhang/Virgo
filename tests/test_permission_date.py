"""Date queries skip review without approving clock changes or shell escapes."""

from __future__ import annotations

import pytest

from virgo.features.permission_scope import command_review_reason, is_read_only_command


@pytest.mark.parametrize("command", [
    "date", "/bin/date", "/usr/bin/date", "date -u", "date +%Y-%m-%d",
    "date -u '+%Y-%m-%d %H:%M:%S'", "date +", "ls -d . && date", "date | cat",
])
def test_date_query_forms_are_read_only(project, command):
    assert is_read_only_command(command)
    assert command_review_reason(command, project, project) == ""


@pytest.mark.parametrize("command", [
    "date -s 2020-01-01", "date -s2020-01-01", "date --set=2020-01-01",
    "date 111109302026", "date -u 111109302026", "date -u -s 2020-01-01",
    "date +%s 111109302026", "date +%s +%Y", "date -u -u", "date --unknown",
    "./date", "/tmp/date", "date $(whoami)", "date; curl https://example.test",
])
def test_date_mutations_and_unknown_forms_still_need_review(project, command):
    assert not is_read_only_command(command)
    assert command_review_reason(command, project, project)


def test_date_query_preserves_workspace_boundaries(project, tmp_path):
    assert command_review_reason("date", tmp_path, project)
    assert command_review_reason("date > ../outside.txt", project, project)
    assert command_review_reason("ls -d /etc && date", project, project)
    assert command_review_reason("PATH=/tmp date", project, project)
    assert not is_read_only_command("date > output.txt")
    assert command_review_reason("date > output.txt", project, project) == ""


def test_home_desktop_and_date_need_no_review():
    from pathlib import Path

    home = Path.home()
    assert command_review_reason("ls -d ~/Desktop && date", home, home) == ""
