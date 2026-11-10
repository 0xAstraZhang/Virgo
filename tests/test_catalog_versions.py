"""Automatic version sources, offline recovery, and credential-free release checks."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from virgo.ai import catalog_versions as versions
from virgo.ai.models import resolve_model


@pytest.fixture(autouse=True)
def no_installed_cli(monkeypatch):
    monkeypatch.setattr(versions.shutil, "which", lambda _: None)


def resolver():
    return versions.CodexCatalogVersion(resolve_model("openai-codex/test"))


def test_official_stable_release_is_cached_without_credentials(monkeypatch):
    original = httpx.Client
    requests = []

    def serve(request):
        requests.append(request)
        assert str(request.url) == versions.CODEX_RELEASE_URL
        assert "authorization" not in request.headers and "chatgpt-account-id" not in request.headers
        return httpx.Response(200, json={"name": "@openai/codex", "version": "0.200.0"})

    monkeypatch.setattr(versions.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(serve), **kw))
    model = resolve_model("openai-codex/test", {"openai-codex": {"apiKey": "secret", "headers": {"x-secret": "secret"}}})
    state = versions.CodexCatalogVersion(model)
    assert state.choose() == "0.200.0"
    assert len(requests) == 1 and "x-secret" not in requests[0].headers
    assert "verified" not in json.loads(state.path.read_text())
    assert resolver().choose() == "0.200.0" and len(requests) == 1
    state.remember("0.200.0")
    cached = state.path.read_text()
    assert json.loads(cached)["verified"] == "0.200.0" and "secret" not in cached


def test_local_updates_periodic_refresh_and_force_refresh(monkeypatch):
    installed = ["0.200.0"]
    published = ["0.210.0"]
    checks = []
    monkeypatch.setattr(versions, "installed_codex_version", lambda: installed[0])
    monkeypatch.setattr(versions.CodexCatalogVersion, "_published_version", lambda _: checks.append(True) or published[0])
    assert resolver().choose() == "0.210.0"
    installed[0] = "0.220.0"
    assert resolver().choose() == "0.220.0" and len(checks) == 1
    published[0] = "0.230.0"
    assert resolver().choose(force=True) == "0.230.0" and len(checks) == 2
    state = resolver()
    state.cache["checked_at"] = time.time() - versions.VERSION_TTL - 1
    published[0] = "0.240.0"
    assert state.choose() == "0.240.0" and len(checks) == 3


def test_offline_uses_installed_or_last_verified_version_without_hardcoded_fallback(monkeypatch):
    def offline(_):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(versions.CodexCatalogVersion, "_published_version", offline)
    with pytest.raises(versions.VersionError, match="No Codex catalog version"):
        resolver().choose()
    monkeypatch.setattr(versions, "installed_codex_version", lambda: "0.210.0")
    state = resolver()
    assert state.choose() == "0.210.0"
    state.remember("0.220.0")
    monkeypatch.setattr(versions, "installed_codex_version", lambda: None)
    assert resolver().choose() == "0.220.0"


def test_explicit_pin_never_probes_or_updates_any_version_source(monkeypatch):
    monkeypatch.setattr(versions, "installed_codex_version", lambda: pytest.fail("must not probe CLI"))
    monkeypatch.setattr(versions.CodexCatalogVersion, "_published_version", lambda _: pytest.fail("must not query registry"))
    model = resolve_model("openai-codex/test", {"openai-codex": {"clientVersion": "1.2.3"}})
    state = versions.CodexCatalogVersion(model)
    assert state.choose(force=True) == "1.2.3"
    state.remember("1.2.3")
    assert not state.path.exists()
    model.options["clientVersion"] = "latest; shell command"
    with pytest.raises(versions.VersionError, match="must be a stable version"):
        versions.CodexCatalogVersion(model).choose()


@pytest.mark.parametrize("stdout, expected", [
    ("codex-cli 0.220.0\n", "0.220.0"), ("codex v1.2.3", "1.2.3"),
    ("codex-cli 0.220.0-alpha.1", None), ("arbitrary 1.2.3", None),
])
def test_cli_version_probe_is_bounded_and_only_parses_codex_output(monkeypatch, stdout, expected):
    monkeypatch.setattr(versions.shutil, "which", lambda _: "/test/codex")

    def run(argv, **kwargs):
        assert argv == ["/test/codex", "--version"]
        assert kwargs["timeout"] == 1.5 and not kwargs.get("shell")
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(versions.subprocess, "run", run)
    assert versions.installed_codex_version() == expected


def test_cli_probe_failure_is_optional(monkeypatch):
    monkeypatch.setattr(versions.shutil, "which", lambda _: "/test/codex")

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("codex", 1.5)

    monkeypatch.setattr(versions.subprocess, "run", timeout)
    assert versions.installed_codex_version() is None


@pytest.mark.parametrize("metadata", [
    {"name": "@openai/codex", "version": "0.220.0-beta.1"},
    {"name": "another-package", "version": "9.0.0"}, {"version": "1.2.3"}, [],
])
def test_invalid_release_metadata_cannot_replace_verified_version(monkeypatch, metadata):
    original = httpx.Client
    monkeypatch.setattr(versions.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=metadata)), **kw))
    state = resolver()
    state.remember("0.210.0")
    assert state.choose(force=True) == "0.210.0"
    assert "published" not in state.cache


def test_bad_cache_and_read_only_cache_do_not_break_local_discovery(monkeypatch):
    state = resolver()
    state.path.parent.mkdir(parents=True)
    state.path.write_text("broken json")
    monkeypatch.setattr(versions, "installed_codex_version", lambda: "0.220.0")
    monkeypatch.setattr(versions.CodexCatalogVersion, "_published_version", lambda _: "0.230.0")

    def readonly(*_):
        raise OSError("read only")

    monkeypatch.setattr(versions.os, "replace", readonly)
    assert resolver().choose() == "0.230.0"


def test_cancelled_lookup_does_not_run_cli_network_or_write_cache(monkeypatch):
    signal = threading.Event()
    signal.set()
    monkeypatch.setattr(versions, "installed_codex_version", lambda: pytest.fail("cancelled"))
    state = versions.CodexCatalogVersion(resolve_model("openai-codex/test"), signal)
    with pytest.raises(versions.VersionCancelled):
        state.choose()
    assert not state.path.exists()
