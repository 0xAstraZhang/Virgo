"""Automatic main-model effort across API login, inference, and cached startup."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler

import pytest

from virgo.ai.catalog import CatalogModel
from virgo.harness.session import AgentSession

from tests.support.harness import RecordingUI
from tests.support.http import bypass_proxies, serve, sse


@pytest.mark.parametrize("metadata,expected", [
    ({"default_reasoning_level": "max"}, "max"),
    ({}, "high"),
])
def test_login_model_effort(project, monkeypatch, metadata, expected):
    bypass_proxies(monkeypatch)
    row = {"id": "deepseek-flash", **metadata}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert self.path == "/models"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": [row]}).encode())

        def do_POST(self):  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = sse([{"id": "1", "object": "chat.completion.chunk", "created": 1, "model": row["id"],
                         "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)

    with serve(Handler) as url:
        ui = RecordingUI()
        ui.interactive = True
        answers = {"Base URL": url, "API key": "test-key"}
        monkeypatch.setattr(ui, "input", lambda question, *, default="", **_: answers.get(question, default))
        session = AgentSession(project, ui=ui, session=None, extensions=False, refresh_models=False)
        try:
            session.prompt("/login api deepseek")
            assert session.model.spec == "deepseek/deepseek-flash"
            session.prompt("/effort")
            assert ui.lines[-1] == f"effort: {expected}"
            session.prompt("hello")
            assert requests[-1]["reasoning_effort"] == expected
            assert "effort" not in json.loads((session.paths.user / "settings.json").read_text())
        finally:
            session.shutdown()

        again = AgentSession(project, session=None, extensions=False, refresh_models=False)
        try:
            assert again.effort == expected
        finally:
            again.shutdown()


@pytest.mark.parametrize("spec,metadata,expected", [
    ("openai/gpt-6-luna", {}, "high"),
    ("openai/gpt-6-luna", {"supported_reasoning_levels": ["low", "max"]}, "low"),
    ("openai/gpt-6-luna", {"reasoning": True, "supported_reasoning_levels": []}, None),
    ("openai/gpt-6-luna", {"reasoning": False}, None),
    ("anthropic/claude-opus-5-5", {"default_reasoning_level": "max"}, "max"),
])
def test_automatic_effort_respects_model_capabilities(make_session, spec, metadata, expected):
    session = make_session(refresh_models=False)
    session.set_model(spec)
    session.remember_model_metadata(session.model, [CatalogModel(session.model.id, metadata)])
    assert session.effort == expected


def test_explicit_effort_survives_a_new_catalog_default(make_session):
    session = make_session(refresh_models=False)
    session.set_model("deepseek/deepseek-flash")
    session.prompt("/effort high")
    session.remember_model_metadata(session.model, [CatalogModel(session.model.id, {"default_reasoning_level": "max"})])
    assert session.effort == "high"
