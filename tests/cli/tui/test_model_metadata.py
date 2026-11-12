"""Startup refresh, cached pickers and restarts through real HTTP and terminal UI."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from textual.widgets import Static

from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.preferences import PreferencePicker
from virgo.harness.preferences import model_choices
from virgo.harness.session import AgentSession

from tests.support.tui import wait_for


@pytest.mark.parametrize("mode", ["live", "override", "missing", "unavailable", "corrupt", "endpoint", "failed_refresh", "invalid", "empty"])
def test_startup_refresh_and_cached_picker(tmp_path, mode):
    requests = []
    gate = threading.Event()
    fail = mode == "unavailable"
    empty = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            gate.wait(5)
            self.send_response(503 if fail else 200)
            self.end_headers()
            row = {"id": "grok-4.7", "created": 300}
            if mode != "missing":
                row.update(context_window=600000, max_output_tokens=32000)
            self.wfile.write(json.dumps({"data": [] if empty else [row, {"id": "old", "created": 100}]}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = {"api": "openai", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "test"}
    other = {**config, "baseUrl": f"http://127.0.0.1:{server.server_port}/other"}
    if mode == "override":
        config["models"] = {"grok-4.7": {"contextWindow": 250000}}
    expected = 250000 if mode == "override" else 500000 if mode in {"missing", "unavailable"} else 600000

    async def scenario():
        nonlocal fail, empty

        def create():
            ui = FullscreenUI()
            session = AgentSession(tmp_path, home=tmp_path / "home", model="local/grok-4.7", session=None,
                                   ui=ui, settings={"providers": {"local": config, "other": other}}, extensions=False)
            return session, VirgoApp(session, ui)

        async def open_picker(app, pilot):
            app.controller.submit("/model")
            await wait_for(pilot, lambda: not app.controller.busy or app.query_one(PreferencePicker).display)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)

        session, app = create()
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                assert not session._catalog_refresh_done.is_set(), "Startup must not wait for a slow directory."
                assert session.model.context_window == (250000 if mode == "override" else 500000)
                await open_picker(app, pilot)
                assert not gate.is_set(), "The UI remains usable while startup discovery is pending."
                gate.set()
                await wait_for(pilot, session._catalog_refresh_done.is_set)
                assert session.model.context_window == expected
                assert sorted(requests) == ["/other/models", "/v1/models"]
                await open_picker(app, pilot)
                await open_picker(app, pilot)
                assert len(requests) == 2, "/model must never fetch."
        finally:
            gate.set()
            session.shutdown()
        cached = list((session.paths.user / "model-metadata").glob("*.json"))
        if mode != "unavailable":
            assert len(cached) == 2
        if mode == "corrupt":
            for path in cached:
                path.write_text("{broken")
        if mode == "invalid":
            for path in cached:
                data = json.loads(path.read_text())
                data["models"]["grok-4.7"]["context_window"] = "bad"
                path.write_text(json.dumps(data))
        if mode == "endpoint":
            config["baseUrl"] += "/different"
        fail = mode != "empty"
        empty = mode == "empty"
        gate.clear()
        before = len(requests)
        restarted, app = create()
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                value = 500000 if mode in {"corrupt", "invalid", "endpoint"} else expected
                assert restarted.model.context_window == value
                assert f"/{value / 1000:.1f}k" in str(app.query_one("#metrics", Static).content)
                gate.set()
                await wait_for(pilot, restarted._catalog_refresh_done.is_set)
                if empty:
                    assert not model_choices(restarted)
                    assert restarted.model.context_window == 500000
                else:
                    assert restarted.model.context_window == value
                    if mode not in {"corrupt", "endpoint", "unavailable"}:
                        offered = [m for m in model_choices(restarted) if m.spec.startswith("local/")]
                        assert [m.label for m in offered] == ["grok-4.7", "old"]
                        assert offered[0].released_at == 300
                await open_picker(app, pilot)
                assert len(requests) == before + 2
        finally:
            gate.set()
            restarted.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        gate.set()
        server.shutdown()
        server.server_close()
        worker.join()
