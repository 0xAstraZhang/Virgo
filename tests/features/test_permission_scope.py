"""Shell command classification: read-only, inside the workspace, or needing review."""

from __future__ import annotations

import pytest

from virgo.features.permission_scope import command_review_reason, is_read_only_command


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


# -- date ----------------------------------------------------------------------------------


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
