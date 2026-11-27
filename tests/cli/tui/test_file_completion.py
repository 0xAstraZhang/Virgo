"""Path completion behavior at the real fullscreen editor boundary."""

from __future__ import annotations

import asyncio
from threading import Event

from textual.widgets import OptionList

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.path_completion import (
    SearchScope,
    display_path,
    matching_paths,
    mention_at,
    quote_path,
    scan_paths,
    search_scope,
)

from tests.support.tui import wait_for


def test_search_scope_and_quoted_mention(project):
    assert mention_at("user@example.com", 0, 16) is None
    mention = mention_at('review @"folder name/fi" now', 1, 23)
    assert mention is not None and mention.query == "folder name/fi"
    assert mention.row == 1
    assert search_scope(project, "sess")[0] == SearchScope(project, True)
    assert search_scope(project, "src/")[0] == SearchScope(project / "src")
    assert search_scope(project, "../")[0] == SearchScope(project.parent)
    assert display_path(project.parent, project, True) == project.parent.as_posix() + "/"


def test_workspace_search_respects_ignore_and_finds_empty_directories(project, tmp_path):
    (project / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    (project / "ignored").mkdir()
    (project / "ignored" / "hidden.py").touch()
    (project / "src").mkdir()
    (project / "src" / "empty").mkdir()
    (project / "src" / "session.py").touch()
    (project / "src" / "session.log").touch()
    (project / "session.py").touch()
    scope = SearchScope(project, True)
    result = scan_paths(scope, project, tmp_path / "private", Event())
    assert result.available
    paths = {entry.path.relative_to(project).as_posix() for entry in result.entries}
    assert {"src", "src/empty", "src/session.py", "session.py"} <= paths
    assert not {"ignored", "ignored/hidden.py", "src/session.log"} & paths
    assert matching_paths(result, scope, "session.py")[0].path == project / "session.py"
    assert matching_paths(result, scope, "ssn")[0].path == project / "session.py"


def test_external_directory_scan_is_shallow_and_excludes_private_paths(project, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    child = external / "child"
    child.mkdir()
    (child / "nested.txt").touch()
    (external / "visible.txt").touch()
    private = external / "private"
    private.mkdir()
    (private / "secret.txt").touch()
    scope = SearchScope(external)
    result = scan_paths(scope, project, private, Event())
    assert {entry.path for entry in result.entries} == {child, external / "visible.txt"}
    assert matching_paths(result, scope, "")[0].path == external
    assert not scan_paths(SearchScope(private), project, private, Event()).available


def test_directory_navigation_empty_directory_and_parent_restore(make_session, project):
    (project / "empty").mkdir()
    (project / "other").mkdir()

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("@")
            await wait_for(pilot, lambda: app.completion_names == ["./", "empty/", "other/"])
            options = app.query_one("#completions", OptionList)
            await pilot.press("down", "right")
            await wait_for(pilot, lambda: app.editor.text == "@empty/" and app.completion_names == ["empty/"])
            await pilot.press("left")
            await wait_for(pilot, lambda: app.completion_names == ["./", "empty/", "other/"])
            assert options.highlighted == 1
            await pilot.press("right")
            await wait_for(pilot, lambda: app.completion_names == ["empty/"])
            await pilot.press("enter")
            assert app.editor.text == "empty/ "
            assert not options.display
            assert not app.session.main.messages
            app.editor.load_text("@empty/")
            app.editor.move_cursor(app.editor.document.end)
            await wait_for(pilot, lambda: app.completion_names == ["empty/"])
            await pilot.press("tab")
            assert app.editor.text == "@empty/"
            assert options.display
            assert not app.session.main.messages
            await pilot.press("enter")
            assert app.editor.text == "empty/ "
            assert not options.display

    asyncio.run(scenario())


def test_tab_opens_directories_and_inserts_files(make_session, project):
    (project / "src").mkdir()
    (project / "src" / "nested").mkdir()
    (project / "src" / "nested" / "target.py").touch()

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("@")
            await wait_for(pilot, lambda: app.completion_names == ["./", "src/"])
            options = app.query_one("#completions", OptionList)
            await pilot.press("tab")
            assert app.editor.text == "@"
            assert options.display
            await pilot.press("down", "tab")
            await wait_for(pilot, lambda: app.editor.text == "@src/"
                           and app.completion_names == ["src/", "src/nested/"])
            await pilot.press("down", "tab")
            await wait_for(pilot, lambda: app.editor.text == "@src/nested/"
                           and app.completion_names == ["src/nested/", "src/nested/target.py"])
            await pilot.press("down", "tab")
            assert app.editor.text == "src/nested/target.py "
            assert not options.display
            assert not app.session.main.messages

    asyncio.run(scenario())


def test_path_completion_replaces_only_active_mention_and_preserves_other_lines(make_session, project):
    (project / "session.py").touch()
    (project / "other file.py").touch()

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(100, 30)) as pilot:
            app.editor.load_text("first line\ncompare @sess with @other")
            app.editor.move_cursor((1, len("compare @sess")))
            await wait_for(pilot, lambda: app.completion_names == ["session.py"])
            await pilot.press("tab")
            assert app.editor.text == "first line\ncompare session.py with @other"
            app.editor.move_cursor(app.editor.document.end)
            await wait_for(pilot, lambda: app.completion_names == ["other file.py"])
            await pilot.press("enter")
            assert app.editor.text == 'first line\ncompare session.py with "other file.py" '
            assert not app.session.main.messages

    asyncio.run(scenario())


def test_external_path_and_no_matches_do_not_submit(make_session, project, tmp_path):
    external = tmp_path / "outside"
    external.mkdir()
    (external / "target.txt").touch()

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(100, 30)) as pilot:
            app.editor.load_text("@../outside/")
            app.editor.move_cursor(app.editor.document.end)
            expected = [external.as_posix() + "/", (external / "target.txt").as_posix()]
            await wait_for(pilot, lambda: app.completion_names == expected)
            await pilot.press("down", "right", "tab")
            path = (external / "target.txt").as_posix()
            assert app.editor.text == quote_path(path, force=path.startswith("/")) + " "
            app.editor.load_text("@no-such-file")
            app.editor.move_cursor(app.editor.document.end)
            await wait_for(pilot, lambda: app.state.path_completion is not None
                           and not app.state.path_completion.pending)
            await pilot.press("enter", "tab")
            assert app.editor.text == "@no-such-file"
            assert not app.session.main.messages
            await pilot.press("escape")
            assert not app.query_one("#completions", OptionList).display
            await pilot.press("left")
            assert app.editor.cursor_location == (0, len("@no-such-file") - 1)

    asyncio.run(scenario())


def test_reopening_picker_refreshes_files(make_session, project):
    old = project / "old.txt"
    old.touch()

    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("@")
            await wait_for(pilot, lambda: "old.txt" in app.completion_names)
            await pilot.press("escape", "backspace")
            old.unlink()
            (project / "new.txt").touch()
            await pilot.press("@")
            await wait_for(pilot, lambda: "new.txt" in app.completion_names)
            assert "old.txt" not in app.completion_names

    asyncio.run(scenario())
