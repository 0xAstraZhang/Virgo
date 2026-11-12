from __future__ import annotations

import pytest

from virgo.ai.providers import anthropic, openai

from tests.support.http import bypass_proxies, fake_api


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
