"""Cancellation at the wire, SDK, session and print-mode boundaries."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path

import anthropic
import httpcore2
import httpx2
import openai
import pytest

from virgo import ai

from tests.support.http import bypass_proxies
from tests.support.sdk_streams import PARTIAL, POST_PATH, PROVIDERS, api_name, cli_args, text_reply


def model(provider, url):
    options = {"apiKey": "local-test-key"}
    if provider == "openai-codex":
        options["authMetadata"] = {"accountId": "local-account"}
    return ai.Model("local", "test", api_name(provider), base_url=url, options=options)


def stream(provider, url, cancel):
    return ai.stream(model(provider, url), ai.Context("", [ai.UserMessage("Hello")]), ai.StreamOptions(signal=cancel))


def sdk_session(make_session, provider, url):
    session = make_session(refresh_models=False, settings={
        "compaction": {"enabled": False}, "plugins": {"web": {"enabled": False}},
    })
    session.main.model = session.model = model(provider, url)
    return session


def stall(server, phase, *, prefix=b"", body=b""):
    # These gates belong to one POST. A later, concurrent request gets independent gates.
    gates = {name: threading.Event() for name in ("started", "release", "peer_closed", "expired")}
    server.update(gates, stall=True, stall_at=phase, prefix=prefix, body=body)
    return gates


@contextmanager
def consuming(open_stream, *, release=None, on_event=None):
    finished = Future()

    def consume():
        try:
            events = []
            for event in open_stream():
                events.append(event)
                if on_event is not None:
                    on_event(event)
            finished.set_result(events)
        except BaseException as exc:
            finished.set_exception(exc)

    worker = threading.Thread(target=consume, name="test-sdk-consumer", daemon=True)
    worker.start()
    try:
        yield finished
    finally:
        if release is not None:
            release.set()
        worker.join(timeout=5)
        assert not worker.is_alive(), "consumer survived peer/gate teardown"


def remaining(deadline):
    return max(0, deadline - time.monotonic())


def cancel_by_deadline(cancel):
    deadline = time.monotonic() + 2
    cancel.set()
    return deadline


def assert_aborted(events, text=""):
    assert [ev.type for ev in events if ev.type in ("retry", "error", "done")] == ["error"]
    assert events[-1].type == "error"
    result = events[-1].partial
    assert result.stop_reason == "aborted"
    assert result.error_kind == "aborted"
    assert result.error_message == "aborted by user"
    assert result.error_details is None
    assert result.text() == text


def assert_peer_and_result(finished, gates, deadline, text=""):
    try:
        events = finished.result(timeout=remaining(deadline))
    except TimeoutError:
        pytest.fail(f"no terminal result within two seconds (peer_closed={gates['peer_closed'].is_set()})")
    assert gates["peer_closed"].wait(remaining(deadline)), "peer still open after cancel"
    assert time.monotonic() < deadline, "terminal delivery and peer EOF exceeded two seconds"
    assert not gates["release"].is_set() and not gates["expired"].is_set()
    assert_aborted(events, text)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("phase", ["before_headers", "after_headers_before_first_event", "after_text_delta"])
def test_cancel_stalled_sdk_stream(server, make_session, provider, phase):
    session = sdk_session(make_session, provider, server["url"])
    cancel, received_delta = threading.Event(), threading.Event()
    wire_phase = {"before_headers": "headers", "after_headers_before_first_event": "body", "after_text_delta": "delta"}
    gates = stall(server, wire_phase[phase], prefix=text_reply(provider, PARTIAL)[0])

    def observe(event):
        if event.type == "text_delta" and event.delta == PARTIAL:
            received_delta.set()

    def open_stream():
        return session.main.call_model([ai.UserMessage("Hello")], cancel)

    with consuming(open_stream, release=gates["release"], on_event=observe) as finished:
        assert gates["started"].wait(10), "SDK did not reach the selected HTTP phase"
        if phase == "after_text_delta":
            assert received_delta.wait(2), "consumer did not receive the flushed delta"
        assert not finished.done()
        assert_peer_and_result(finished, gates, cancel_by_deadline(cancel), PARTIAL if phase == "after_text_delta" else "")
        assert [r["path"].split("?")[0] for r in server["requests"]] == [POST_PATH[provider]]
        assert session.diagnostics.recent() == []


@pytest.mark.parametrize("provider", (*PROVIDERS, "openai-codex"))
@pytest.mark.parametrize("proxy", [False, True], ids=["direct", "proxy"])
def test_cancel_stalled_tls_handshake(monkeypatch, provider, proxy):
    bypass_proxies(monkeypatch)
    cancel = threading.Event()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(10)
        url = f"https://127.0.0.1:{listener.getsockname()[1]}"
        if proxy:
            monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{listener.getsockname()[1]}")
            url = "https://upstream.invalid"
        with consuming(lambda: stream(provider, url, cancel)) as finished:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(4)
                if proxy:
                    headers = b""
                    while not headers.endswith(b"\r\n\r\n"):
                        chunk = peer.recv(4096)
                        assert chunk
                        headers += chunk
                    assert headers.startswith(b"CONNECT upstream.invalid:443 ")
                    peer.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                assert peer.recv(1) == b"\x16", "expected a TLS handshake record"
                deadline = cancel_by_deadline(cancel)
                events = finished.result(timeout=remaining(deadline))
                # Drain the rest of ClientHello and require EOF without sending ServerHello.
                while True:
                    peer.settimeout(remaining(deadline))
                    if not peer.recv(8192):
                        break
                assert time.monotonic() < deadline
                assert_aborted(events)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_cancel_stalled_connect(monkeypatch, provider):
    bypass_proxies(monkeypatch)
    entered, exited, release, cancel = (threading.Event() for _ in range(4))
    tasks = []

    def sync_connect(*args, **kwargs):
        entered.set()
        try:
            release.wait(4)
            raise OSError("test connect gate released")
        finally:
            exited.set()

    async def async_connect(*args, **kwargs):
        tasks.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=4)
        finally:
            exited.set()

    # Gate the installed transport before it obtains any socket. Exercise both the
    # existing sync SDK and the async SDK without depending on OS SYN/DNS timeouts.
    monkeypatch.setattr(httpcore2.SyncBackend, "connect_tcp", sync_connect)
    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", async_connect)
    with consuming(lambda: stream(provider, "https://127.0.0.1:1", cancel), release=release) as finished:
        assert entered.wait(10)
        deadline = cancel_by_deadline(cancel)
        events = finished.result(timeout=remaining(deadline))
        assert exited.is_set(), "connect operation outlived the result"
        assert all(task.done() for task in tasks)
        assert time.monotonic() < deadline
        assert_aborted(events)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("session_owned", [False, True], ids=["standalone", "session"])
def test_pre_aborted_stream_starts_no_io(monkeypatch, make_session, provider, session_owned):
    bypass_proxies(monkeypatch)
    cancel = threading.Event()
    requests, threads = [], []
    start = threading.Thread.start

    def record_thread(thread):
        threads.append(thread)
        return start(thread)

    def unexpected_request(client, request, **kwargs):
        requests.append(request)
        raise AssertionError("pre-aborted SDK request")

    url = "http://127.0.0.1:1"
    if session_owned:
        session = sdk_session(make_session, provider, url)
        dispatch = ai.stream

        def pre_abort(model, context, opts):
            # Let Runtime assemble its actual session-owned options. Cancel at dispatch
            # so the runtime's own early return cannot hide eager adapter I/O startup.
            assert opts.signal is cancel
            cancel.set()
            return dispatch(model, context, opts)

        monkeypatch.setattr(ai, "stream", pre_abort)
        open_stream = partial(session.main.call_model, [ai.UserMessage("Hello")], cancel)
    else:
        cancel.set()
        open_stream = partial(stream, provider, url, cancel)
    monkeypatch.setattr(threading.Thread, "start", record_thread)
    monkeypatch.setattr(httpx2.Client, "send", unexpected_request)
    monkeypatch.setattr(httpx2.AsyncClient, "send", unexpected_request)
    started = time.monotonic()
    events = list(open_stream())
    assert time.monotonic() - started < 2
    assert requests == [] and threads == []
    assert_aborted(events)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_completed_stream_wins_over_late_cancel(server, provider):
    server["body"] = b"".join(text_reply(provider, "Complete"))
    cancel = threading.Event()
    retained = stream(provider, server["url"], cancel)
    events = iter(retained)
    for event in events:
        if event.type in ("done", "error"):
            break
    assert event.type == "done"
    cancel.set()  # Exactly at terminal delivery, before the next iterator advance.
    assert list(events) == []
    result = retained.result()
    assert result is event.partial and result.stop_reason == "stop"
    assert result.text() == "Complete" and (result.usage.input, result.usage.output) == (12, 4)
    assert result.error_details is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_truncated_stream_without_cancel_retains_diagnostics(server, provider):
    server.update(body=text_reply(provider, PARTIAL)[0], drop=True, response_headers={"x-request-id": "req-cut"})
    events = list(stream(provider, server["url"], threading.Event()))
    assert [ev.type for ev in events if ev.type in ("error", "done")] == ["error"]
    result = events[-1].partial
    assert (result.stop_reason, result.error_kind, result.text()) == ("error", "retryable", PARTIAL)
    details = result.error_details
    assert details["code"] == "PROVIDER_STREAM_FAILED" and details["phase"] == "provider.stream"
    assert details["error_id"] and details["context"]["api"] == api_name(provider)
    assert details["context"]["http"]["request_id"] == "req-cut"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("clear_after_close", [False, True], ids=["set", "latched"])
def test_cancel_with_backpressure(server, provider, monkeypatch, clear_after_close):
    paused, resume, cancel = (threading.Event() for _ in range(3))
    count = 512
    gates = stall(server, "delta", prefix=text_reply(provider, "x", count=count)[0])
    sdk_stream = anthropic.AsyncStream if provider == "anthropic" else openai.AsyncStream
    read_events = sdk_stream._iter_events
    read_count = 0

    async def counted(self):
        nonlocal read_count
        async for event in read_events(self):
            read_count += 1
            yield event

    monkeypatch.setattr(sdk_stream, "_iter_events", counted)

    def pause_on_first_delta(event):
        if event.type == "text_delta" and not paused.is_set():
            paused.set()
            assert resume.wait(4), "test did not resume consumer"

    with consuming(lambda: stream(provider, server["url"], cancel), release=gates["release"],
                   on_event=pause_on_first_delta) as finished:
        try:
            assert paused.wait(10) and gates["started"].wait(2)
            # The whole burst is on the wire. Observe SDK read-ahead plateau *inside*
            # the burst while the consumer is paused; a fully drained burst is unbounded.
            first_delta = {"openai": 1, "anthropic": 3, "responses": 2}[provider]
            until = time.monotonic() + 1
            saturated = False
            while time.monotonic() < until:
                previous = read_count
                time.sleep(0.05)
                if first_delta < read_count < count and read_count == previous:
                    saturated = True
                    break
            deadline = cancel_by_deadline(cancel)
            if clear_after_close:
                assert gates["peer_closed"].wait(remaining(deadline))
                cancel.clear()  # The producer already observed it while the client was paused.
            resume.set()
            assert_peer_and_result(finished, gates, deadline, "x")
            assert saturated, f"SDK read {read_count} events while consumer paused"
        finally:
            resume.set()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_session_reuses_client_and_keeps_sibling_alive(server, make_session, monkeypatch, provider):
    session = sdk_session(make_session, provider, server["url"])
    sibling = session.create_runtime("sibling", "child", model=session.model)
    sdk = anthropic.AsyncAnthropic if provider == "anthropic" else openai.AsyncOpenAI
    request = sdk.request
    send = httpcore2.AsyncConnectionPool.handle_async_request
    calls = []
    pools = set()
    workers = []
    start = threading.Thread.start

    async def observe(client, *args, **kwargs):
        calls.append((client, asyncio.current_task(), threading.current_thread()))
        return await request(client, *args, **kwargs)

    async def observe_pool(pool, req):
        pools.add(pool)
        return await send(pool, req)

    def record_worker(thread):
        parent = threading.current_thread()
        if parent.name == "test-sdk-consumer" or parent in workers:
            workers.append(thread)
        return start(thread)

    monkeypatch.setattr(sdk, "request", observe)
    monkeypatch.setattr(httpcore2.AsyncConnectionPool, "handle_async_request", observe_pool)
    monkeypatch.setattr(threading.Thread, "start", record_worker)
    messages = [ai.UserMessage("Hello")]
    good = b"".join(text_reply(provider, "Complete"))

    def pending_tasks():
        # Snapshot from inside the owner loop; it may have a persistent driver task.
        result = Future()
        loop = calls[0][1].get_loop()
        loop.call_soon_threadsafe(lambda: result.set_result(asyncio.all_tasks(loop)))
        return result.result(timeout=2)

    server["body"] = good
    with consuming(partial(session.main.call_model, messages, threading.Event())) as warmup:
        assert warmup.result(timeout=10)[-1].type == "done"
    idle_tasks = pending_tasks() if calls else set()
    idle_workers = set(workers)
    healthy = stall(server, "body", body=good)
    with consuming(lambda: sibling.call_model(messages, threading.Event()), release=healthy["release"]) as other:
        assert healthy["started"].wait(10)
        for _ in range(2):
            gates = stall(server, "headers")
            cancel = threading.Event()
            with consuming(partial(session.main.call_model, messages, cancel), release=gates["release"]) as interrupted:
                assert gates["started"].wait(3)
                assert_peer_and_result(interrupted, gates, cancel_by_deadline(cancel))
                assert calls[-1][1].done(), "cancelled request task survived terminal delivery"
                # httpcore2 retains the pool request until its body stream is closed.
                assert sum(len(pool._requests) for pool in pools) == 1  # Only the healthy sibling.
            assert not other.done() and not healthy["peer_closed"].is_set()
        healthy["release"].set()
        sibling_events = other.result(timeout=2)
        assert not healthy["expired"].is_set()
        assert sibling_events[-1].type == "done" and sibling_events[-1].partial.text() == "Complete"

    server.update(stall=False, body=good)
    events = list(session.main.call_model(messages, threading.Event()))
    assert events[-1].type == "done" and events[-1].partial.text() == "Complete"
    assert len(server["requests"]) == len(calls) == 5
    assert session.diagnostics.recent() == []

    client, _, loop_thread = calls[0]
    assert loop_thread.is_alive()
    assert all(c is client and owner is loop_thread and task.done() for c, task, owner in calls)
    assert not pending_tasks() - idle_tasks
    assert not any(thread.is_alive() for thread in set(workers) - idle_workers)
    assert pools and all(not pool._requests for pool in pools)
    executor = calls[0][1].get_loop()._default_executor
    warm_pool_workers = idle_workers.intersection(executor._threads) if executor is not None else set()
    session.shutdown()
    assert client.is_closed()
    assert not loop_thread.is_alive()
    assert not any(thread.is_alive() for thread in set(workers) - warm_pool_workers)
    # loop.close() signals the default executor to stop without joining its idle threads.
    deadline = time.monotonic() + 2
    for thread in warm_pool_workers:
        thread.join(remaining(deadline))
        if thread.is_alive():
            frame = sys._current_frames().get(thread.ident)
            stack = "".join(traceback.format_stack(frame)) if frame else "(stack unavailable)"
            pytest.fail(f"Default executor thread {thread.name} survived shutdown:\n{stack}")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX subprocess SIGINT delivery")
@pytest.mark.parametrize("provider", PROVIDERS)
def test_print_sigint_during_stalled_sdk_stream(server, project, provider):
    args = cli_args(Path(os.environ["VIRGO_HOME"]), provider, server["url"])
    gates = stall(server, "delta", prefix=text_reply(provider, PARTIAL)[0])
    process = subprocess.Popen([*args, "-p", "Hello"], cwd=project, stdin=subprocess.DEVNULL)
    try:
        assert gates["started"].wait(15), f"CLI did not reach the stall (exit={process.poll()})"
        deadline = time.monotonic() + 2
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=remaining(deadline)) == 130
        assert time.monotonic() < deadline and not gates["expired"].is_set()
        assert len(server["requests"]) == 1
    finally:
        gates["release"].set()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_cancel_preserves_multiplexed_h2_sibling(make_session, monkeypatch, provider):
    pytest.importorskip("h2", reason="optional HTTP/2 transport")
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, StreamEnded

    bypass_proxies(monkeypatch)

    def prior_knowledge(init):
        def configure(transport, *args, **kwargs):
            return init(transport, *args, **{**kwargs, "http1": False, "http2": True})
        return configure

    for transport in (httpx2.HTTPTransport, httpx2.AsyncHTTPTransport):
        monkeypatch.setattr(transport, "__init__", prior_knowledge(transport.__init__))

    cancel, other_cancel, received_delta = (threading.Event() for _ in range(3))
    # Close accepted and listening sockets before joining either consumer on failure.
    with ExitStack() as consumers, socket.create_server(("127.0.0.1", 0)) as listener, ExitStack() as sockets:
        listener.settimeout(10)
        session = sdk_session(make_session, provider, f"http://127.0.0.1:{listener.getsockname()[1]}")
        sibling = session.create_runtime("sibling", "child", model=session.model)
        messages = [ai.UserMessage("Hello")]

        def observe(event):
            if event.type == "text_delta":
                received_delta.set()

        first = consumers.enter_context(consuming(partial(session.main.call_model, messages, cancel), on_event=observe))
        peer = sockets.enter_context(listener.accept()[0])
        sockets.callback(cancel.set)
        sockets.callback(other_cancel.set)
        connection = H2Connection(config=H2Configuration(client_side=False))
        connection.initiate_connection()
        peer.sendall(connection.data_to_send())

        def receive_request():
            deadline = time.monotonic() + 4
            while True:
                peer.settimeout(remaining(deadline))
                data = peer.recv(65536)
                assert data, "HTTP/2 connection closed before sibling request"
                events = connection.receive_data(data)
                for event in events:
                    if isinstance(event, DataReceived):
                        connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                peer.sendall(connection.data_to_send())
                for event in events:
                    if isinstance(event, StreamEnded):
                        return event.stream_id

        first_id = receive_request()
        headers = [(":status", "200"), ("content-type", "text/event-stream")]
        connection.send_headers(first_id, headers)
        connection.send_data(first_id, text_reply(provider, PARTIAL)[0])
        peer.sendall(connection.data_to_send())
        assert received_delta.wait(2)
        second = consumers.enter_context(consuming(partial(sibling.call_model, messages, other_cancel)))
        second_id = receive_request()  # Same accepted TCP socket, different HTTP/2 stream.
        assert second_id != first_id
        connection.send_headers(second_id, headers)
        peer.sendall(connection.data_to_send())

        deadline = cancel_by_deadline(cancel)
        assert_aborted(first.result(timeout=remaining(deadline)), PARTIAL)
        assert time.monotonic() < deadline and not second.done()
        connection.send_data(second_id, b"".join(text_reply(provider, "Sibling")), end_stream=True)
        peer.sendall(connection.data_to_send())
        result = second.result(timeout=2)
        assert result[-1].type == "done" and result[-1].partial.text() == "Sibling"
