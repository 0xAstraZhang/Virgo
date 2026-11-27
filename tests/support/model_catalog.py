"""A gated model directory shared by session and fullscreen integration tests."""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from threading import Event

from virgo.harness.session import AgentSession

from tests.support.http import bypass_proxies, serve


@contextmanager
def model_catalog(monkeypatch):
    bypass_proxies(monkeypatch)
    state = {
        "requests": [], "gate": Event(), "status": 200,
        "models": [
            {"id": "grok-4.7", "created": 300, "context_window": 600000, "max_output_tokens": 32000},
            {"id": "old", "created": 100},
        ],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            state["gate"].wait(5)
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(json.dumps({"data": state["models"]}).encode())

        def log_message(self, *args):
            pass

    with serve(Handler, join_handlers=True) as base_url:
        state["providers"] = {
            name: {"api": "openai", "baseUrl": f"{base_url}/{path}", "apiKey": "test"}
            for name, path in (("local", "v1"), ("other", "other"))
        }
        try:
            yield state
        finally:
            state["gate"].set()


def catalog_session(tmp_path, catalog, ui):
    # Metadata tests exercise the session without probing or executing host shells.
    return AgentSession(
        tmp_path, home=tmp_path / "home", model="local/grok-4.7", session=None, ui=ui,
        settings={"execution": {"mode": "no-limit"}, "providers": catalog["providers"]},
        extensions=False, shell_catalog=(),
    )
