"""Regression scenarios for workspace trust and MCP response leases.
Failure modes checked: busy TUI blocks selection; workspace trust is reset;
extension stays unloaded; recovery evidence is rejected; response snapshot
keeps old transport; connection only changes at a subsequent request.
"""

import asyncio
import json
import sys
import threading
from pathlib import Path

from textual.widgets import OptionList

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.sessions import SessionPicker

from tests.support.harness import tool_results
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


def test_mcp_response_snapshot(make_session, tmp_path):
    server = tmp_path / "server.py"
    source = Path("tests/support/mcp_echo_server.py").read_text()
    source = source.replace(
        'if name == "echo":',
        'if name == "echo" and args["text"] == "fail":\n            reply(rid, {"content": [{"type": "text", "text": "temporary failure"}], "isError": True})\n        elif name == "echo":',
    )
    server.write_text(source)
    s = make_session(
        refresh_models=False,
        settings={"mcpServers": {"echo": {"command": sys.executable, "args": [str(server)], "timeout": 5}}},
    )
    # Real tools/call failure creates real authorized recovery evidence.
    faux.script(
        "test", faux.reply(tools=[("mcp__echo__echo", {"text": "fail"})]), faux.reply("failure observed")
    )
    s.prompt("call server")
    error_id = tool_results(s)[-1].details["error"]["error_id"]
    old = s.services["mcp"]["echo"]
    child = s.create_runtime("child", "subagent")
    child.model = s.resolve("faux/child")
    entered, release = threading.Event(), threading.Event()

    def slow_model(ctx):
        entered.set()
        assert release.wait(15)
        return faux.reply(tools=[("mcp__echo__echo", {"text": "child response"})])

    faux.script(
        "child",
        slow_model,
        faux.reply(tools=[("mcp__echo__echo", {"text": "next request"})]),
        faux.reply("done"),
    )
    worker = threading.Thread(target=lambda: child.agent.prompt("use echo"))
    worker.start()
    try:
        assert entered.wait(10)
        assert child.agent.running
        assert child.tools and s.tool("mcp__echo__echo") in child.tools
        faux.script(
            "test",
            faux.reply(tools=[("mcp_reconnect", {"server": "echo", "error_id": error_id})]),
            faux.reply("recovered"),
        )
        s.prompt("recover server")
        recovery = tool_results(s)[-1]
        assert not recovery.is_error, recovery.text()
        assert not old._closing
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        result = next(m for m in child.messages if isinstance(m, ToolResultMessage))
        print(
            "MCP_RESULT",
            json.dumps(
                {
                    "response_snapshot_generation": old.generation,
                    "actual_call_generation": result.details["transport"]["generation"],
                    "old_client_closed": old._closing,
                    "call_success": not result.is_error,
                    "recovery": recovery.details["mcp_recovery"],
                }
            ),
        )
        results = [m for m in child.messages if isinstance(m, ToolResultMessage)]
        assert [m.details["transport"]["generation"] for m in results] == [old.generation, old.generation + 1]
        assert all(not m.is_error for m in results)
        assert old._closing
    finally:
        release.set()
        worker.join(10)
        s.release_runtime(child)
