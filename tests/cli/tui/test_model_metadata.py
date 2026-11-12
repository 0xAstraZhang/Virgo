"""Model metadata flows from a real HTTP directory to a fresh terminal session."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from textual.widgets import Static

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.preferences import PreferencePicker
from virgo.harness.session import AgentSession

from tests.support.tui import wait_for


@pytest.mark.parametrize("mode", ["live", "override", "missing", "unavailable", "corrupt", "endpoint", "failed_refresh", "invalid", "empty"])
def test_fresh_session_uses_model_metadata(tmp_path, mode):
    requests = []
    fail = False
    empty = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(503 if mode == "unavailable" or fail else 200)
            self.end_headers()
            row = {"id": "grok-4.7"}
            if mode != "missing":
                row.update(context_window=600000, max_output_tokens=32000)
            self.wfile.write(json.dumps({"data": [] if empty else [row]}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = {"api": "openai", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "test"}
    if mode == "override":
        config["models"] = {"grok-4.7": {"contextWindow": 250000}}
    expected = 250000 if mode == "override" else 500000 if mode in {"missing", "unavailable"} else 600000

    async def scenario():
        nonlocal fail, empty
        def create():
            ui = FullscreenUI()
            session = AgentSession(tmp_path, home=tmp_path / "home", model="local/grok-4.7", session=None,
                                   ui=ui, settings={"providers": {"local": config}}, extensions=False)
            return session, VirgoApp(session, ui)

        session, app = create()
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                assert not requests, "Startup must not fetch model metadata."
                assert session.model.context_window == (250000 if mode == "override" else 500000)
                app.controller.submit("/model")
                await wait_for(pilot, lambda: not app.controller.busy or app.query_one(PreferencePicker).display)
                await pilot.press("escape")
                await wait_for(pilot, lambda: not app.controller.busy)
                if mode in {"failed_refresh", "empty"}:
                    fail = mode == "failed_refresh"
                    empty = mode == "empty"
                    app.controller.submit("/model")
                    await wait_for(pilot, lambda: not app.controller.busy or app.query_one(PreferencePicker).display)
                    await pilot.press("escape")
                    await wait_for(pilot, lambda: not app.controller.busy)
        finally:
            session.shutdown()
        cached = list((session.paths.user / "model-metadata").glob("*.json"))
        if mode != "unavailable":
            assert cached
        if mode == "corrupt":
            cached[0].write_text("{broken")
        if mode == "invalid":
            data = json.loads(cached[0].read_text())
            data["models"]["grok-4.7"]["context_window"] = "bad"
            cached[0].write_text(json.dumps(data))
        if mode == "endpoint":
            config["baseUrl"] += "/different"
        before = len(requests)
        restarted, app = create()
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                value = 500000 if mode in {"corrupt", "invalid", "endpoint", "empty"} else expected
                assert restarted.model.context_window == value
                assert f"/{value / 1000:.1f}k" in str(app.query_one("#metrics", Static).content)
                restarted.set_model("local/grok-4.7")
                assert restarted.model.context_window == value
                assert len(requests) == before, "Restart and direct selection must stay offline."
        finally:
            restarted.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
