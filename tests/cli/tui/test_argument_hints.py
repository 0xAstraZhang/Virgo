"""Argument hints exercised through the assembled application."""
import asyncio

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI


def rendered(app):
    return app.editor.render_line(0).text


def test_hint_keyboard_lifecycle(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*'/mo', 'tab')
            assert app.editor.text == '/model '
            assert '[provider/id]' in rendered(app)
            await pilot.press('right', 'shift+right')
            assert app.editor.text == '/model '
            await pilot.press('x')
            assert '[provider/id]' not in rendered(app)
            await pilot.press('backspace')
            assert '[provider/id]' in rendered(app)
            await pilot.press('left')
            assert '[provider/id]' not in rendered(app)
            await pilot.press('end')
            assert '[provider/id]' in rendered(app)
            await pilot.press('shift+home')
            assert '[provider/id]' not in rendered(app)
            assert app.editor.selected_text == '/model '
            await pilot.press('end', 'ctrl+j')
            assert '[provider/id]' not in rendered(app)
    asyncio.run(scenario())


def test_hint_resolution_and_clipping(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        session.templates['review'] = ('review', '$ARGUMENTS')
        session.templates['help'] = ('collision', '$ARGUMENTS')
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            for command, hint in [('model', '[provider/id]'), ('review', '[arguments]'),
                                  ('help', ''), ('unknown', '')]:
                app.editor.load_text('/' + command)
                app.editor.move_cursor(app.editor.document.end)
                await pilot.pause()
                assert ('[' in rendered(app)) == bool(hint)
                if hint:
                    assert hint in rendered(app)
            app.editor.load_text('/model   ')
            app.editor.move_cursor(app.editor.document.end)
            await pilot.pause()
            assert '[provider/id]' in rendered(app)
            height = app.editor.size.height
            await pilot.resize_terminal(16, 24)
            assert app.editor.render_line(0).cell_length <= app.editor.content_size.width
            assert app.editor.size.height == height
            await pilot.resize_terminal(80, 24)
            assert '[provider/id]' in rendered(app)
    asyncio.run(scenario())


def test_feature_hints(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test() as pilot:
            for name in ('login', 'logout', 'shell', 'effort', 'compact', 'sessions',
                         'search', 'permissions', 'goal', 'memory', 'tasks', 'workflow', 'mcp'):
                app.editor.load_text('/' + name)
                app.editor.move_cursor(app.editor.document.end)
                await pilot.pause()
                assert '[' in rendered(app), name
    asyncio.run(scenario())


def test_real_extensions_copy_submit_history_and_focus(make_session, project, monkeypatch):
    extensions = project / '.virgo' / 'extensions'
    extensions.mkdir(parents=True)
    (extensions / 'hints.py').write_text('''
def setup(api):
    def capture(session, args):
        session.services['captured'] = args
    api.register_command('old', capture, 'legacy', True)
    api.register_command('model', capture, 'override')
    api.register_command('capture', capture, argument_hint='[中文参数很长很长很长]')
''')
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        session.templates['model'] = ('collision', '$ARGUMENTS')
        app = VirgoApp(session, ui)
        copied = []
        monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
        async with app.run_test() as pilot:
            for name in ('old', 'model'):
                app.editor.load_text('/' + name)
                app.editor.move_cursor(app.editor.document.end)
                await pilot.pause()
                assert '[' not in rendered(app)
            assert session.commands.get('old').concurrent
            app.editor.load_text('/capture')
            app.editor.move_cursor(app.editor.document.end)
            await pilot.pause()
            assert '[中文参数' in rendered(app)
            assert any(seg.style and str(seg.style.color) == "Color('bright_black', ColorType.STANDARD, number=8)" for seg in app.editor.render_line(0))
            app.set_focus(None)
            await pilot.pause()
            assert '[' not in rendered(app)
            app.editor.focus()
            await pilot.press('shift+home')
            app.editor.action_copy()
            assert copied == ['/capture']
            await pilot.press('end', 'enter')
            from tests.support.tui import wait_for
            await wait_for(pilot, lambda: 'captured' in session.services)
            assert session.services['captured'] == ''
            assert app.history.entries[-1] == '/capture'
            assert '[中文' not in app.history.path.read_text()
            await pilot.press('up')
            assert app.editor.text == '/capture'
            assert '[中文参数' in rendered(app)
            assert not app.completion_names
    asyncio.run(scenario())


def test_workspace_switch_uses_current_command_metadata(make_session, project, tmp_path):
    from tests.cli.tui.test_session_switching import saved_session
    from tests.support.tui import wait_for

    target = tmp_path / 'target'
    path = saved_session(target)
    for root, hint in ((project, '[first]'), (target, '[second]')):
        directory = root / '.virgo' / 'extensions'
        directory.mkdir(parents=True)
        (directory / 'hint.py').write_text(
            'def setup(api):\n'
            f'    api.register_command("custom", lambda s,a: None, argument_hint="{hint}")\n'
        )
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test() as pilot:
            await pilot.press(*'/custom')
            assert '[first]' in rendered(app)
            await app.change_conversation(path)
            await wait_for(pilot, lambda: app.session.cwd == target)
            await pilot.press(*'/custom')
            assert '[second]' in rendered(app)
            assert '[first]' not in rendered(app)
    asyncio.run(scenario())


def test_hint_undo_redo_and_soft_wrap(make_session):
    async def scenario():
        ui = FullscreenUI()
        app = VirgoApp(make_session(ui=ui), ui)
        async with app.run_test() as pilot:
            await pilot.press(*'/model ')
            app.editor.history.checkpoint()
            await pilot.press('x')
            assert '[' not in rendered(app)
            await pilot.press('ctrl+z')
            assert app.editor.text == '/model '
            assert '[' in rendered(app)
            await pilot.press('ctrl+y')
            assert app.editor.text == '/model x'
            assert '[' not in rendered(app)
            await pilot.press('backspace')
            await pilot.resize_terminal(7, 24)
            assert '[' not in rendered(app)
            await pilot.resize_terminal(80, 24)
            assert '[provider/id]' in rendered(app)
    asyncio.run(scenario())


def test_real_terminal_argument_hint(project, tmp_path):
    import pytest

    from tests.support.terminal import HAS_PTY, PtyClient, plain

    if not HAS_PTY:
        pytest.skip('requires POSIX PTY')
    with PtyClient(project, tmp_path / 'hint-home') as terminal:
        terminal.wait_for(lambda data: 'Enter send' in plain(data))
        terminal.send(b'/model ')
        terminal.wait_for(lambda data: '[provider/id]' in plain(data))
        terminal.resize(24, 24)
        terminal.pump(0.2)
        terminal.resize(80, 24)
        terminal.pump(0.2)
        terminal.send(b'\x03')
        terminal.send(b'/exit\r')
        assert terminal.finish() == 0
        terminal.assert_restored()
