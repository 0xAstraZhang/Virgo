"""Model metadata flows from a real HTTP directory to a fresh terminal session."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from textual.widgets import Static

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.harness.session import AgentSession


@pytest.mark.parametrize("mode", ["live", "override", "missing", "unavailable"])
def test_fresh_session_uses_model_metadata(tmp_path, mode):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(503 if mode == "unavailable" else 200)
            self.end_headers()
            row = {"id": "grok-4.7"}
            if mode != "missing":
                row.update(context_window=600000, max_output_tokens=32000)
            self.wfile.write(json.dumps({"data": [row]}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = {"api": "openai", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "test"}
    if mode == "override":
        config["models"] = {"grok-4.7": {"contextWindow": 250000}}
    expected = {"live": 600000, "override": 250000, "missing": 500000, "unavailable": 500000}[mode]

    async def scenario():
        ui = FullscreenUI()
        session = AgentSession(tmp_path, home=tmp_path / "home", model="local/grok-4.7", session=None,
                               ui=ui, settings={"providers": {"local": config}}, extensions=False)
        try:
            app = VirgoApp(session, ui)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                assert session.model.context_window == expected
                assert f"/{expected / 1000:.1f}k" in str(app.query_one("#metrics", Static).content)
                session.set_model("local/grok-4.7")
                assert session.model.context_window == expected
                assert not session.main.messages
                assert requests == ["/v1/models"]
        finally:
            session.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
