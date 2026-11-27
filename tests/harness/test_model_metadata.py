"""Startup metadata, cache invalidation and offline choices through real HTTP."""

import json

import pytest

from virgo.harness.preferences import model_choices

from tests.support.harness import PickerUI, wait_until
from tests.support.model_catalog import catalog_session, model_catalog


@pytest.mark.parametrize("mode", ["live", "override", "missing", "unavailable", "corrupt", "endpoint", "invalid", "empty"])
def test_startup_catalog_cache_and_restart(tmp_path, monkeypatch, mode):
    with model_catalog(monkeypatch) as catalog:
        if mode == "unavailable":
            catalog["status"] = 503
        if mode == "missing":
            catalog["models"][0] = {"id": "grok-4.7", "created": 300}
        if mode == "override":
            catalog["providers"]["local"]["models"] = {"grok-4.7": {"contextWindow": 250000}}
        expected = 250000 if mode == "override" else 500000 if mode in {"missing", "unavailable"} else 600000
        ui = PickerUI()
        session = catalog_session(tmp_path, catalog, ui)
        try:
            assert not session.models.refresh_done.is_set(), "Startup must not wait for a slow directory."
            assert session.model.context_window == (250000 if mode == "override" else 500000)
            wait_until(lambda: len(catalog["requests"]) == 2)
            catalog["gate"].set()
            assert session.models.refresh_done.wait(3)
            assert session.model.context_window == expected
            assert sorted(catalog["requests"]) == ["/other/models", "/v1/models"]
            session.prompt("/model")
            session.prompt("/model")
            if mode != "unavailable":
                assert len(ui.requests) == 2
                assert "local/grok-4.7" in {model.spec for model in ui.requests[-1].models}
            else:
                assert not ui.requests
            assert len(catalog["requests"]) == 2, "/model must never fetch."
        finally:
            catalog["gate"].set()
            session.shutdown()

        cached = list((session.paths.user / "model-metadata").glob("*.json"))
        assert len(cached) == (0 if mode == "unavailable" else 2)
        if mode == "corrupt":
            for path in cached:
                path.write_text("{broken")
        if mode == "invalid":
            for path in cached:
                data = json.loads(path.read_text())
                data["models"]["grok-4.7"]["context_window"] = "bad"
                path.write_text(json.dumps(data))
        if mode == "endpoint":
            catalog["providers"]["local"]["baseUrl"] += "/different"
        catalog["status"] = 200 if mode == "empty" else 503
        if mode == "empty":
            catalog["models"] = []
        catalog["gate"].clear()
        restarted = catalog_session(tmp_path, catalog, PickerUI())
        try:
            value = 500000 if mode in {"corrupt", "invalid", "endpoint"} else expected
            assert restarted.model.context_window == value
            catalog["gate"].set()
            assert restarted.models.refresh_done.wait(3)
            if mode == "empty":
                assert not model_choices(restarted)
                assert restarted.model.context_window == 500000
            else:
                assert restarted.model.context_window == value
                if mode not in {"corrupt", "endpoint", "unavailable"}:
                    offered = [m for m in model_choices(restarted) if m.spec.startswith("local/")]
                    assert [m.label for m in offered] == ["grok-4.7", "old"]
                    assert offered[0].released_at == 300
            restarted.prompt("/model")
            assert len(catalog["requests"]) == 4
            if mode == "endpoint":
                assert catalog["requests"].count("/v1/different/models") == 1
        finally:
            catalog["gate"].set()
            restarted.shutdown()
