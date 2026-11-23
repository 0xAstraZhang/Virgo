from __future__ import annotations

import pytest

from virgo.ai.providers import anthropic, openai
from virgo.ai.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

from tests.support.http import bypass_proxies, fake_api


@pytest.fixture(params=["mixed", "image-only", "interleaved", "text", "empty", "non-vision"])
def responses_read(request, pixel_png):
    """Canonical history and the expected Responses output for either transport."""
    caption = TextContent("Image: pixel.png")
    png = ImageContent(pixel_png, "image/png")
    text = {"type": "input_text", "text": "Image: pixel.png"}
    image = {"type": "input_image", "image_url": f"data:image/png;base64,{pixel_png}", "detail": "auto"}
    blocks, output = {
        "mixed": ([caption, png], [text, image]),
        "image-only": ([png], [image]),
        "interleaved": ([caption, png, TextContent("crop"), ImageContent("Yg==", "image/jpeg")], [
            text, image, {"type": "input_text", "text": "crop"},
            {"type": "input_image", "image_url": "data:image/jpeg;base64,Yg==", "detail": "auto"},
        ]),
        "text": ([TextContent(" first "), TextContent("last\n")], " first \nlast\n"),
        "empty": ([], "(no output)"),
        "non-vision": ([caption, png], "Image: pixel.png\n[image omitted: reader does not accept images]"),
    }[request.param]
    history = [
        UserMessage("read the image"),
        AssistantMessage([ToolCall("call_1|fc_1", "read", {"path": "pixel.png"})]),
        ToolResultMessage("call_1|fc_1", "read", blocks),
    ]
    return history, request.param != "non-vision", {"type": "function_call_output", "call_id": "call_1", "output": output}


def _clear_sdk_clients() -> None:
    # SDK clients are cached per endpoint; a later test may get the same ephemeral port.
    anthropic._clients.clear()
    openai._clients.clear()


@pytest.fixture
def server(monkeypatch):
    """A loopback provider API; see ``tests.support.http.fake_api`` for the state a test scripts."""
    bypass_proxies(monkeypatch)
    _clear_sdk_clients()
    with fake_api() as state:
        yield state
    _clear_sdk_clients()
