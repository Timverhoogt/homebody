"""Native sleep transport tests: no SDK construction, media or hardware."""

import json
from uuid import uuid4

import httpx
import pytest

from homebody import daemon_sleep


class Socket:
    def __init__(self, events):
        self.events = iter(events)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recv(self, *, timeout):
        assert 0 < timeout <= 12.0
        event = next(self.events, TimeoutError("held completion"))
        if isinstance(event, Exception):
            raise event
        return json.dumps(event)


def transport(monkeypatch, events, *, post_failure=False, cancel_failure=False, payload=None):
    move_id = str(uuid4())
    calls = []

    def connect(url, **kwargs):
        assert url == "ws://127.0.0.1:8000/api/move/ws/updates"
        assert kwargs["proxy"] is None
        calls.append("subscribe")
        return Socket(events(move_id))

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 2.0, "trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            if url.endswith("/play/goto_sleep"):
                if post_failure:
                    raise httpx.ReadTimeout("admission unknown")
                body = {"uuid": move_id} if payload is None else payload
                return httpx.Response(200, json=body, request=httpx.Request("POST", url))
            assert url.endswith("/stop")
            assert kwargs == {"json": {"uuid": move_id}}
            return httpx.Response(500 if cancel_failure else 200, json={}, request=httpx.Request("POST", url))

        def get(self, url):
            calls.append(("get", url))
            assert url.endswith("/running")
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(daemon_sleep, "connect", connect)
    monkeypatch.setattr(daemon_sleep.httpx, "Client", Client)
    return calls


def test_subscribe_before_single_native_admission_and_wait_for_matching_completion(monkeypatch):
    calls = transport(monkeypatch, lambda mid: [
        {"type": "move_completed", "uuid": str(uuid4())},
        {"type": "move_started", "uuid": mid},
        {"type": "move_completed", "uuid": mid},
    ])
    daemon_sleep.run_daemon_sleep()
    assert calls == ["subscribe", ("post", "http://127.0.0.1:8000/api/move/play/goto_sleep", {})]


@pytest.mark.parametrize("kind", ["move_failed", "move_cancelled"])
def test_failure_and_cancel_are_not_completion(monkeypatch, kind):
    calls = transport(monkeypatch, lambda mid: [{"type": kind, "uuid": mid}])
    with pytest.raises(daemon_sleep.DaemonSleepError) as error:
        daemon_sleep.run_daemon_sleep()
    assert error.value.uncertain is False
    assert len(calls) == 2


def test_timeout_cancels_only_our_move_and_verifies_no_pending_owner(monkeypatch):
    calls = transport(monkeypatch, lambda mid: [])
    with pytest.raises(daemon_sleep.DaemonSleepError) as error:
        daemon_sleep.run_daemon_sleep()
    assert error.value.uncertain is False
    assert calls[-2][1].endswith("/stop")
    assert calls[-1][1].endswith("/running")


def test_unconfirmed_cancellation_leaves_outcome_unknown(monkeypatch):
    transport(monkeypatch, lambda mid: [], cancel_failure=True)
    with pytest.raises(daemon_sleep.DaemonSleepError) as error:
        daemon_sleep.run_daemon_sleep()
    assert error.value.uncertain is True


def test_ambiguous_admission_is_never_retried(monkeypatch):
    calls = transport(monkeypatch, lambda mid: [], post_failure=True)
    with pytest.raises(daemon_sleep.DaemonSleepError) as error:
        daemon_sleep.run_daemon_sleep()
    assert error.value.uncertain is True
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [{}, {"uuid": "not-a-uuid"}, []])
def test_malformed_admission_fails_closed_without_repairing_identifiers(monkeypatch, payload):
    calls = transport(monkeypatch, lambda mid: [], payload=payload)
    with pytest.raises(daemon_sleep.DaemonSleepError) as error:
        daemon_sleep.run_daemon_sleep()
    assert error.value.uncertain is True
    assert len(calls) == 2


def test_subscription_failure_never_admits_movement(monkeypatch):
    def fail(*args, **kwargs):
        raise ConnectionRefusedError("offline")
    monkeypatch.setattr(daemon_sleep, "connect", fail)
    with pytest.raises(daemon_sleep.DaemonSleepError) as error:
        daemon_sleep.run_daemon_sleep()
    assert error.value.uncertain is False


def test_real_loopback_http_and_websocket_complete_even_before_post_returns(monkeypatch):
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from websockets.sync.server import serve

    admitted, registered = threading.Event(), threading.Event()
    outgoing = queue.Queue()
    mid = str(uuid4())
    paths = []

    def updates(ws):
        registered.set()
        while True:
            event = outgoing.get(timeout=5)
            if event is None:
                return
            ws.send(json.dumps(event))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            paths.append(self.path)
            assert registered.wait(2)
            # Complete before the caller knows its UUID, as a no-motion sleep can.
            outgoing.put({"type": "move_completed", "uuid": str(uuid4())})
            outgoing.put({"type": "move_started", "uuid": mid})
            outgoing.put({"type": "move_completed", "uuid": mid})
            admitted.set()
            body = json.dumps({"uuid": mid}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with serve(updates, "127.0.0.1", 0) as server:
        ws_port = server.socket.getsockname()[1]
        ws_thread = threading.Thread(target=server.serve_forever, daemon=True)
        ws_thread.start()
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http_thread = threading.Thread(target=http.serve_forever, daemon=True)
        http_thread.start()
        monkeypatch.setattr(daemon_sleep, "_UPDATES", f"ws://127.0.0.1:{ws_port}/api/move/ws/updates")
        monkeypatch.setattr(daemon_sleep, "_HTTP", f"http://127.0.0.1:{http.server_port}/api/move")
        try:
            daemon_sleep.run_daemon_sleep()
            assert admitted.is_set()
            assert paths == ["/api/move/play/goto_sleep"]
        finally:
            outgoing.put(None)
            http.shutdown()
            http.server_close()
            http_thread.join(3)
            server.shutdown()
            ws_thread.join(3)
