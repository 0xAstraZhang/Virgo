"""Verify image and text tool results from the CLI to the Codex wire format."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from tests.support.http import bypass_proxies, serve, sse

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/P5sAAAAASUVORK5CYII="


@pytest.mark.parametrize("vision", [True, False])
def test_cli_preserves_supported_tool_images_in_request_history(project, monkeypatch, vision):
    # Cover dropped images, a lost text caption or call ID, changed text-only
    # wire format, lost image on the next turn, and image leakage to text models.
    bypass_proxies(monkeypatch)
    (project / "sample.png").write_bytes(base64.b64decode(PNG))
    (project / "note.txt").write_text("plain tool result", encoding="utf-8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            index = len(requests) - 1
            if index < 2:
                item = {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}",
                        "name": "read", "arguments": json.dumps({"path": ["sample.png", "note.txt"][index]})}
            else:
                item = {"type": "message", "id": "msg_1", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}]}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(sse([
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"status": "completed", "output": [item]}},
            ], done=False))

        def log_message(self, *_args):
            pass

    with serve(Handler) as url:
        home = Path(os.environ["VIRGO_HOME"])
        (home / "settings.json").write_text(json.dumps({"providers": {"local": {
            "api": "openai-codex", "baseUrl": url, "models": {"probe": {"vision": vision}},
        }}}), encoding="utf-8")
        (home / "auth.json").write_text(json.dumps({"local": {
            "type": "api_key", "key": "mock-key", "accountId": "mock-account",
        }}), encoding="utf-8")
        subprocess.run(
            [sys.executable, "-m", "virgo", "-p", "--model", "local/probe", "--no-session", "--no-extensions",
             "--profile", "minimal", "--permission-mode", "accept-all", "Read both files."],
            cwd=project, input="", capture_output=True, text=True, check=True, timeout=15,
        )

    assert len(requests) == 3
    outputs = [
        {item["call_id"]: item["output"] for item in request["input"] if item.get("type") == "function_call_output"}
        for request in requests
    ]
    assert outputs[0] == {}
    assert outputs[1]["call_0"] == outputs[2]["call_0"]
    assert outputs[2]["call_1"] == "plain tool result"
    if vision:
        caption, image = outputs[2]["call_0"]
        assert caption["type"] == "input_text" and "sample.png" in caption["text"]
        assert image == {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}", "detail": "auto"}
    else:
        assert "does not accept images" in outputs[2]["call_0"]
        assert PNG not in outputs[2]["call_0"]
