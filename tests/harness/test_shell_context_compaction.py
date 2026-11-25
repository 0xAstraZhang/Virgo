import threading

import pytest

from virgo.ai.providers import faux
from virgo.tools.output import MAX_BYTES, MAX_LINES

from tests.support.harness import calls, tool_results, wait_until
from tests.support.sandbox import python_command, run
from tests.support.shell import selected


def wire_result():
    return next(m for m in reversed(calls()[-1]["messages"]) if m["role"] == "toolResult")


@pytest.mark.parametrize("code", [0, 7])
@pytest.mark.parametrize("kind", ["lines", "bytes"])
def test_shell_request_budget(make_session, code, kind):
    session = make_session()
    if kind == "lines":
        stdout = f"'x\\n' * {MAX_LINES + 5}"
    else:
        stdout = f"'x' * {MAX_BYTES + 500}"
    result = run(session, python_command(session, f"print({stdout}, end=''); raise SystemExit({code})"))
    wire = wire_result()
    preview = "\n".join(block["text"] for block in wire["content"])
    assert result.text() == preview
    assert wire["details"]["exit_code"] == code
    assert wire["details"]["output_id"]
    assert wire["is_error"] == bool(code)
    assert len(preview.encode()) <= MAX_BYTES
    assert len(preview.splitlines()) <= MAX_LINES


def test_timeout_with_output(make_session):
    session = make_session()
    command = python_command(session, "import time; print('partial timeout', flush=True); time.sleep(4)")
    result = run(session, command, timeout=1)
    assert result.details["status"] == "timeout"
    assert result.is_error
    assert "partial timeout" in result.text()
    assert wire_result()["is_error"]
    assert "timed out" in wire_result()["content"][0]["text"]


def test_cancel_with_output_remains_a_failure_on_next_request(make_session, project):
    session = make_session()
    marker = project / "started"
    source = (
        "import pathlib, time; print('partial cancellation', flush=True); "
        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(10)"
    )
    cmd = python_command(session, source)
    faux.script("test", faux.reply(tools=[(selected(session).tool_name, {"command": cmd})]))
    worker = threading.Thread(target=session.prompt, args=("start",))
    worker.start()
    try:
        wait_until(marker.exists)
    finally:
        session.main.agent.abort()
        worker.join(timeout=5)
    assert not worker.is_alive()
    result = tool_results(session)[-1]
    assert result.details["status"] == "aborted"
    assert result.details["cancelled"]
    assert result.is_error
    assert "partial cancellation" in result.text()
    faux.script("test", faux.reply("resumed"))
    session.prompt("resume")
    assert wire_result()["is_error"]
    assert "partial cancellation" in wire_result()["content"][0]["text"]


def test_overflow_consumes_only_one_additional_normal_request(make_session):
    session = make_session(faux.error("context exceeded", kind="overflow"),
                           faux.reply("summarized"),
                           faux.error("temporary", kind="retryable"),
                           faux.reply("retried again"),
                           settings={"retry": {"maxRetries": 1}})
    session.prompt("continue")
    requests = [r for r in calls() if "context summarization assistant" not in r["system"]]
    assert len(requests) == 2


def test_overflow_usage_retained_when_summary_is_cancelled(make_session):
    session = make_session(faux.error("context exceeded", kind="overflow"), faux.reply(stop_reason="aborted"))
    session.prompt("continue")
    assert len(calls()) == 2
    [summary] = [r for r in session.store.custom("model_call") if r["purpose"] == "compaction"]
    assert summary["usage"]["input"] > 0
    prior = [
        m for m in session.store.custom("model_call") if m.get("error_kind") == "overflow"
    ]
    assert len(prior) == 1
    assert prior[0]["usage"]["input"] > 0


def log_settings(trigger, **overrides):
    config = {"startTokens": trigger - 2000, "reserveTokens": 3000, "excerptChars": 120}
    return {"compaction": {"triggerTokens": trigger, "keepRecentTokens": 100, "shellLogs": {**config, **overrides}}}


def shell_reply(session, text, call_id, *, code=0):
    command = python_command(session, f"print({text}); raise SystemExit({code})")
    return faux.reply(tools=[(selected(session).tool_name, {"command": command}, call_id)])


def wire_text(request, call_id):
    matches = [m for m in request["messages"] if m["role"] == "toolResult" and m["tool_call_id"] == call_id]
    assert len(matches) == 1
    return "\n".join(block["text"] for block in matches[0]["content"])


def summary_or(*ordinary):
    replies = iter(ordinary)

    def respond(context):
        if "context summarization assistant" in context.system:
            return faux.reply("Carry on with the original goal and retain the test constraint.")
        return next(replies)

    return respond


def test_one_user_turn_avoids_summary_and_preserves_the_latest_batch(make_session):
    def execute(enabled):
        session = make_session()
        trigger = session.main.context_tokens(estimate_only=True) + 20_000
        for key, value in log_settings(trigger)["compaction"].items():
            if key != "shellLogs" or enabled:
                session.settings.set(f"compaction.{key}", value)
        respond = summary_or(
            shell_reply(session, "'old tail ' * 7000", "first"),
            shell_reply(session, "'new tail ' * 7000", "second"),
            faux.reply("finished"),
        )
        faux.script("test", *([respond] * 4))
        session.prompt("Run the checks in order and retain this constraint.")
        return session, list(calls())

    baseline, base_requests = execute(False)
    candidate, requests = execute(True)
    assert len(base_requests) == 4
    assert sum("context summarization assistant" in r["system"] for r in base_requests) == 1
    assert len(requests) == 3
    assert not any("context summarization assistant" in r["system"] for r in requests)
    first, second = tool_results(candidate)
    short = wire_text(requests[-1], "first")
    assert short.endswith(first.text()[-120:]) and first.details["output_id"] in short
    assert wire_text(requests[-1], "second") == second.text()
    assert wire_text(requests[1], "first") == first.text()
    assert len(first.text()) > 45_000
    assert baseline.store.last_compaction() is not None
    [saved] = candidate.store.custom("shell_log_replacements")
    [key] = saved
    assert tool_results(candidate)[0].text() == candidate.store.by_id[key]["message"]["content"][0]["text"]
    [decision] = candidate.store.custom("compaction_decision")
    assert decision["estimated_before"] >= decision["threshold"] > decision["estimated_after"]
    assert decision["shortened_count"] == 1
    assert decision["request_type"] == "ordinary"


@pytest.mark.parametrize("signed", [False, True])
def test_resume_branch_scope_and_log_removal(make_session, signed):
    session = make_session(persist=True, extensions=False, refresh_models=False)
    trigger = session.main.context_tokens(estimate_only=True) + 20_000
    settings = log_settings(trigger)
    for key, value in settings["compaction"].items():
        session.settings.set(f"compaction.{key}", value)
    faux.script("test",
                shell_reply(session, "'first tail ' * 7000", "first"),
                shell_reply(session, "'second tail ' * 7000", "second"),
                faux.reply("done", thinking="signed after shortening" if signed else None))
    session.prompt("Run the two checks.")
    path = session.store.path
    first, _ = tool_results(session)
    output_id = first.details["output_id"]
    short = wire_text(calls()[-1], "first")
    [entry] = [entry for entry in session.store.branch() if entry.get("kind") == "shell_log_replacements"]
    session.shutdown()

    resumed = make_session(session=path, settings=settings, extensions=False, refresh_models=False)
    faux.script("test", faux.reply("still here"))
    resumed.prompt("Check the old state.")
    assert wire_text(calls()[-1], "first") == short
    assert len(resumed.store.custom("shell_log_replacements")) == 1

    resumed.main.execution_context.outputs.lookup(output_id).unlink()
    # Leave space for the restored preview while checking the actual request.
    resumed.settings.set("compaction.triggerTokens", 100_000)
    faux.script("test", faux.reply("log removed"), faux.reply("still removed"))
    resumed.prompt("Verify the evidence state.")
    request = calls()[-1]
    if signed:
        assert wire_text(request, "first") == short
        assert any("Full Shell log is unavailable" in m["content"] for m in request["messages"] if m["role"] == "user")
    else:
        assert wire_text(request, "first").startswith(first.text())
        assert "Full Shell log is unavailable" in wire_text(request, "first")
    resumed.prompt("Continue.")
    assert len(resumed.store.custom("shell_log_replacements")) == 1
    assert len(tool_results(resumed)) == 2

    resumed.store.leaf = entry["parent_id"]
    sibling = resumed._new_main(resumed.store)
    assert not resumed.store.custom("shell_log_replacements")
    faux.script("test", faux.reply("sibling branch"))
    sibling.agent.prompt("Resume from before replacement.")
    assert wire_text(calls()[-1], "first") == first.text()


def test_signature_minted_over_full_history_blocks_new_replacement(make_session):
    session = make_session()
    trigger = session.main.context_tokens(estimate_only=True) + 20_000
    for key, value in log_settings(trigger)["compaction"].items():
        session.settings.set(f"compaction.{key}", value)
    second = shell_reply(session, "'second tail ' * 7000", "second")
    second.thinking = "signed with the full first preview"
    respond = summary_or(shell_reply(session, "'first tail ' * 7000", "first"), second, faux.reply("done"))
    faux.script("test", *([respond] * 4))
    session.prompt("Run the checks.")
    assert not session.store.custom("shell_log_replacements")
    [decision] = [entry for entry in session.store.custom("compaction_decision") if entry["layer"] == 2]
    assert decision["candidate_count"] == 0
    assert decision["request_type"] == "compaction"


@pytest.mark.parametrize("above", [False, True])
def test_no_candidates_uses_third_layer_threshold(make_session, above):
    session = make_session()
    base = session.main.context_tokens(estimate_only=True)
    trigger = base + 20_000
    for key, value in log_settings(trigger, startTokens=base + 8000)["compaction"].items():
        session.settings.set(f"compaction.{key}", value)
    body = "dialog " * (12_500 if above else 7_000)
    respond = summary_or(faux.reply(body), faux.reply("retained"))
    faux.script("test", respond, respond, respond)
    session.prompt("Initial question.")
    session.prompt("Continue.")
    [decision] = [entry for entry in session.store.custom("compaction_decision") if entry["layer"] == 2]
    assert decision["candidate_count"] == decision["shortened_count"] == 0
    assert decision["request_type"] == ("compaction" if above else "ordinary")
    assert bool(session.store.last_compaction()) is above


def test_can_still_compact_after_shortening(make_session):
    session = make_session()
    base = session.main.context_tokens(estimate_only=True)
    trigger = base + 20_000
    for key, value in log_settings(trigger)["compaction"].items():
        session.settings.set(f"compaction.{key}", value)
    second = shell_reply(session, "'new tail ' * 7000", "second")
    second.text = "prior findings " * 2800
    respond = summary_or(shell_reply(session, "'old tail ' * 7000", "first"), second, faux.reply("finished"))
    faux.script("test", *([respond] * 5))
    session.prompt("Run the checks.")
    [saved] = session.store.custom("shell_log_replacements")
    assert len(saved) == 1
    decision = next(entry for entry in session.store.custom("compaction_decision") if entry["layer"] == 2)
    assert decision["estimated_before"] > decision["estimated_after"] >= decision["threshold"]
    assert decision["request_type"] == "compaction"
    assert sum("context summarization assistant" in request["system"] for request in calls()) == 1


def test_manual_summary_retained_tail_reapplies_original_message_record(make_session):
    session = make_session(persist=True)
    trigger = session.main.context_tokens(estimate_only=True) + 20_000
    settings = log_settings(trigger)
    for key, value in settings["compaction"].items():
        session.settings.set(f"compaction.{key}", value)
    faux.script("test",
                shell_reply(session, "'old tail ' * 7000", "first"),
                shell_reply(session, "'new tail ' * 7000", "second"),
                faux.reply("finished"))
    session.prompt("Run checks and keep this constraint.")
    want = wire_text(calls()[-1], "first")
    session.settings.set("compaction.keepRecentTokens", 24_000)
    session.settings.set("compaction.triggerTokens", 100_000)
    faux.script("test", faux.reply("Retain the user constraint."))
    assert session.compact()["kept"] >= 4
    path = session.store.path
    session.shutdown()

    resumed = make_session(session=path, settings={"compaction": {"triggerTokens": 100_000}})
    faux.script("test", faux.reply("done after restart"))
    resumed.prompt("Resume.")
    assert wire_text(calls()[-1], "first") == want
    assert len(resumed.store.custom("shell_log_replacements")) == 1
    assert len(tool_results(resumed)) == 2


def test_parallel_results_keep_failures_and_rewritten_tool_results(make_session):
    session = make_session()
    base = session.main.context_tokens(estimate_only=True)
    for key, value in log_settings(base + 31_000)["compaction"].items():
        session.settings.set(f"compaction.{key}", value)

    def result_hook(result, event, _session):
        if event.call_id == "rewritten":
            # Final metadata alone must not make a failed command eligible.
            result.is_error = False
            result.details.update(status="ok", exit_code=0)
        return result

    session.bus.on("tool_result", result_hook)
    first = shell_reply(session, "'old tail ' * 7000", "eligible")
    first.tool_calls += shell_reply(session, "'nonzero'", "nonzero", code=7).tool_calls
    first.tool_calls += shell_reply(session, "'short output'", "no-id").tool_calls
    first.tool_calls += shell_reply(session, "'rewritten ' * 6000", "rewritten", code=7).tool_calls
    respond = summary_or(first, shell_reply(session, "'latest tail ' * 7000", "latest"), faux.reply("finished"))
    faux.script("test", *([respond] * 5))
    session.prompt("Run the checks.")
    ordinary = [request for request in calls() if "context summarization assistant" not in request["system"]]
    assert len(ordinary) == 3
    results = {m.tool_call_id: m for m in tool_results(session)}
    assert results["rewritten"].details["output_id"]
    assert not results["rewritten"].is_error
    for call_id in ("nonzero", "no-id", "rewritten", "latest"):
        assert wire_text(ordinary[-1], call_id) == results[call_id].text()
    assert "Earlier output shortened" in wire_text(ordinary[-1], "eligible")


def test_invalid_start_watermark_is_reported(make_session):
    session = make_session(settings=log_settings(1000, startTokens=1001))
    session.prompt("Check compaction settings.")
    assert "startTokens exceeds" in session.main.messages[-1].error_message
    assert not calls()


def test_reported_overage_still_shortens_when_local_baseline_already_has_reserve(make_session, monkeypatch):
    session = make_session()
    base = session.main.context_tokens(estimate_only=True)
    trigger = base + 34_000
    for key, value in log_settings(trigger)["compaction"].items():
        session.settings.set(f"compaction.{key}", value)
    faux_events = faux._events

    def inflate_usage(responder, context, result, options):
        for event in faux_events(responder, context, result, options):
            if event.type == "done" and any(call.id == "second" for call in result.tool_calls()):
                result.usage.input = trigger + 1000
            yield event

    monkeypatch.setattr(faux, "_events", inflate_usage)
    respond = summary_or(
        shell_reply(session, "'old tail ' * 7000", "first"),
        shell_reply(session, "'new tail ' * 7000", "second"),
        faux.reply("done"),
    )
    faux.script("test", *([respond] * 4))
    session.prompt("Run both checks.")
    [decision] = session.store.custom("compaction_decision")
    assert decision["estimated_before"] < trigger - decision["reserve_tokens"]
    assert decision["shortened_count"] == 1
    assert decision["request_type"] == "ordinary"
    assert "Earlier output shortened" in wire_text(calls()[-1], "first")
