"""Diagnostic identities follow the failing operation across retries and session switches."""

from __future__ import annotations

import threading

import pytest

from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, Model
from virgo.cli.console import event_json
from virgo.harness import compaction, preferences


def test_startup_and_end_hooks_belong_to_the_new_run(make_session):
    session = make_session(faux.reply("first"), faux.reply("second"), refresh_models=False)
    session.prompt("first turn")
    previous_run, previous_call = session.main.run_id, session.main._model_call_id

    def before(runtime, prompts):
        assert runtime._model_call_id is None
        assert runtime._attempt_number == 0
        raise RuntimeError("startup hook failed")

    def after(runtime):
        raise RuntimeError("end hook failed")

    session.bus.on("before_agent_start", before)
    session.bus.on("agent_end", after)
    session.prompt("second turn")
    failures = {record["message"]: record for record in session.diagnostics.recent()}
    startup = failures["startup hook failed"]["context"]
    ended = failures["end hook failed"]["context"]
    assert startup["run_id"] == ended["run_id"] == session.main.run_id != previous_run
    assert startup["model_call_id"] is None
    assert ended["model_call_id"] == session.main._model_call_id != previous_call
    assert session.main.agent.last_text == "second"


def test_direct_child_prompt_initializes_a_fresh_run_and_clears_previous_call(make_session):
    session = make_session(faux.reply("first"), faux.reply("second"), refresh_models=False)
    child = session.create_runtime("worker", "subagent", tools=[])
    starts = []

    def observe(event):
        if event.type == "agent_start":
            starts.append((child.run_id, child._model_call_id, child._attempt_number))

    child.agent.subscribe(observe)
    child.agent.prompt("one")
    first_call = child._model_call_id
    child.agent.prompt("two")
    assert starts[0][0] and starts[1][0] and starts[0][0] != starts[1][0]
    assert starts[0][1:] == starts[1][1:] == (None, 0)
    assert child._model_call_id != first_call


def test_recovered_retry_exposes_its_log_reference_in_text_and_json(make_session, monkeypatch):
    session = make_session(faux.error("temporary", kind="retryable"), faux.reply("recovered"),
                           refresh_models=False)
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)
    session.prompt("work")
    record = session.diagnostics.recent()[0]
    assert record["error_id"] in session.ui.text
    assert record["log_path"] in session.ui.text
    retries = [event_json(event) for event in session.ui.events
               if event.type == "message_update" and event.stream_event.type == "retry"]
    assert len(retries) == 1
    assert retries[0]["error_details"]["error_id"] == record["error_id"]
    assert retries[0]["error_details"]["context"]["attempt"] == 1
    assert session.main.messages[-1].stop_reason == "stop"
    assert session.main.agent.last_text == "recovered"


def test_retry_reports_stderr_fallback_when_its_log_cannot_be_written(make_session, monkeypatch, capsys):
    session = make_session(faux.error("temporary", kind="retryable"), faux.reply("recovered"),
                           refresh_models=False)
    monkeypatch.setattr(session.main.agent.signal, "wait", lambda _: False)

    def unavailable(*args):
        raise OSError("diagnostic directory unavailable")

    monkeypatch.setattr(session.diagnostics, "_rotate", unavailable)
    session.prompt("work")
    record = session.diagnostics.recent()[0]
    assert record["error_id"] in session.ui.text
    assert "details were sent to stderr" in session.ui.text
    assert record["error_id"] in capsys.readouterr().err
    assert session.main.agent.last_text == "recovered"


@pytest.mark.parametrize("running", [False, True])
def test_native_compaction_uses_its_own_call_id_and_actual_api(make_session, monkeypatch, running):
    session = make_session(faux.reply("earlier answer"), refresh_models=False)
    session.prompt("earlier request")
    runtime = session.main
    generation_call, generation_run = runtime._model_call_id, runtime.run_id
    runtime._attempt_number = 3
    model = Model("openai", "test", "openai")

    def failed(*args, **kwargs):
        return AssistantMessage(provider="openai", model="test", api="openai-responses",
                                stop_reason="error", error_message="native compaction failed")

    monkeypatch.setattr(compaction, "native_compact", failed)
    monkeypatch.setattr(runtime.agent, "running", running)
    assert runtime.compact(model=model, reason="overflow" if running else "manual") is None
    record = session.diagnostics.recent()[0]
    context = record["context"]
    assert context["purpose"] == "compaction"
    assert context["model_call_id"] != generation_call and context["model_call_id"]
    assert context["attempt"] == 1
    assert context["api"] == "openai-responses"
    assert context["run_id"] == (generation_run if running else None)
    assert runtime._model_call_id == generation_call and runtime._attempt_number == 3
    assert session.store.custom("model_call")[-1]["model_call_id"] == context["model_call_id"]


def test_catalog_cache_write_failure_keeps_the_refresh_owner_after_switch(make_session, monkeypatch):
    session = make_session(refresh_models=False, settings={"providers": {
        "local": {"api": "openai", "baseUrl": "http://localhost:9999/v1", "apiKey": "test-key"},
    }})
    owner = session.store
    entered, release = threading.Event(), threading.Event()

    def fetch(model, signal):
        entered.set()
        assert release.wait(3)
        return []

    def fail_save(*args):
        raise OSError("catalog cache write failed")

    monkeypatch.setattr(preferences, "BUILTIN_PROVIDERS", {})
    monkeypatch.setattr(preferences, "fetch_model_catalog", fetch)
    monkeypatch.setattr("virgo.harness.session.write_metadata", fail_save)
    session._catalog_refresh_done.clear()
    preferences.start_model_refresh(session)
    try:
        assert entered.wait(3)
        session.new_session()
    finally:
        release.set()
    assert session._catalog_refresh_done.wait(3)
    records = [record for record in session.diagnostics.recent() if record["phase"] == "catalog.save"]
    assert len(records) == 1
    assert records[0]["context"]["session_id"] == owner.id != session.store.id
