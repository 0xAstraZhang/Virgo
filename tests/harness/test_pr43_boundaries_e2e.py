"""Regression scenario for workspace trust in the TUI.

Failure modes checked: busy TUI blocks selection; workspace trust is reset;
the extension stays unloaded in the opened workspace.
"""

import asyncio
import json

from textual.widgets import OptionList

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.sessions import SessionPicker

from tests.support.tui import wait_for


def test_tui_busy_cross_workspace(make_session, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    saved = make_session(cwd=other, persist=True, trust_project_extensions=False, refresh_models=False)
    saved.prompt("saved conversation in B")
    path = saved.store.path
    saved.shutdown()
    ext = other / ".virgo" / "extensions"
    ext.mkdir(parents=True)
    marker = other / "executed.txt"
    (ext / "probe.py").write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n')
    ui = FullscreenUI()
    session = make_session(
        faux.reply("slow response", delay=20),
        ui=ui,
        persist=True,
        trust_project_extensions=True,
        refresh_models=False,
    )

    async def scenario():
        app = VirgoApp(session, ui, initial="long request in A")
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_for(pilot, lambda: session.main.agent.running)
            assert not app.can_change_session()
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            choices = app.screen.query_one(OptionList)
            choices.highlighted = next(
                i for i, option in enumerate(choices.options) if option.id == str(path)
            )
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.session.cwd == other)
            print(
                "TUI_RESULT",
                json.dumps(
                    {
                        "workspace": str(app.session.cwd),
                        "inherited_trust": app.session.trust_project_extensions,
                        "extension_executed": marker.exists(),
                        "panes": len(app._panes),
                        "old_still_running": session.main.agent.running,
                    }
                ),
            )
            assert not marker.exists()
            assert not app.session.trust_project_extensions
            assert session.main.agent.running
            for pane in app._panes:
                pane.controller.request_close()

    asyncio.run(scenario())
