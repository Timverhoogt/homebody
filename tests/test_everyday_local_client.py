"""Robot client tool turns preserve auth, identity, currentness and exact results."""

import json
from types import SimpleNamespace

import pytest

from homebody.config import AppConfig
from homebody.everyday_client import everyday_session, everyday_url
from homebody.hermes_client import AgentBrokerContext, HermesBridgeClient, HermesBridgeError
from homebody.live_client import LiveBridgeSession


def context():
    return AgentBrokerContext("conversation", False, False, "awake", True, False, True, 7, 7, False)


class Socket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, **_):
        return json.dumps(next(self.events))


def test_pipeline_local_tool_roundtrip_auth_and_verified_result(monkeypatch):
    socket = Socket(
        [
            {"type": "everyday.tool_call", "call_id": "a", "name": "get_reachy_volume", "arguments": {}},
            {"type": "everyday.answer", "text": "Volume is 30 percent."},
        ]
    )
    connected = []
    monkeypatch.setattr("homebody.everyday_client.connect", lambda *a, **k: connected.append((a, k)) or socket)
    called = []
    result = everyday_session(
        AppConfig(api_key="private", bridge_url="https://host.example/prefix"),
        "how loud?",
        session_id="voice-a",
        context=context(),
        local_tools=["get_reachy_volume"],
        executor=lambda *a: called.append(a) or {"volume": 30, "verified": True},
        is_current=lambda: True,
    )
    assert result == "Volume is 30 percent."
    assert connected[0][0] == ("wss://host.example/prefix/v1/everyday/session",)
    assert connected[0][1]["additional_headers"]["Authorization"] == "Bearer private"
    assert called == [("get_reachy_volume", {}, "a")]
    assert socket.sent[0]["agent_context"]["session_generation"] == 7
    assert socket.sent[1] == {
        "type": "everyday.tool_result",
        "call_id": "a",
        "result": {"volume": 30, "verified": True},
    }


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "everyday.tool_call", "call_id": "a", "name": "shell", "arguments": {}}],
        [
            {"type": "everyday.tool_call", "call_id": "a", "name": "get_reachy_volume", "arguments": {}},
            {"type": "everyday.tool_call", "call_id": "a", "name": "get_reachy_volume", "arguments": {}},
        ],
    ],
)
def test_unknown_or_replayed_tool_cannot_execute(monkeypatch, events):
    socket = Socket(events)
    monkeypatch.setattr("homebody.everyday_client.connect", lambda *a, **k: socket)
    called = []
    with pytest.raises(HermesBridgeError):
        everyday_session(
            AppConfig(),
            "test",
            session_id="a",
            context=context(),
            local_tools=["get_reachy_volume"],
            executor=lambda *a: called.append(a) or {},
            is_current=lambda: True,
        )
    assert len(called) <= 1


def test_stop_during_local_call_never_returns_late_answer(monkeypatch):
    socket = Socket(
        [
            {"type": "everyday.tool_call", "call_id": "a", "name": "get_reachy_volume", "arguments": {}},
            {"type": "everyday.answer", "text": "late"},
        ]
    )
    monkeypatch.setattr("homebody.everyday_client.connect", lambda *a, **k: socket)
    current = [True]

    def execute(*_):
        current[0] = False
        return {"ok": True}

    with pytest.raises(HermesBridgeError, match="cancelled"):
        everyday_session(
            AppConfig(),
            "test",
            session_id="a",
            context=context(),
            local_tools=["get_reachy_volume"],
            executor=execute,
            is_current=lambda: current[0],
        )
    assert len(socket.sent) == 1


def test_live_result_is_local_protocol_not_realtime_provider_event():
    session = LiveBridgeSession(AppConfig())
    sent = []
    session._socket = SimpleNamespace(send=lambda v: sent.append(json.loads(v)))
    session.send_everyday_result("one", {"ok": True, "volume": 30})
    assert sent == [{"type": "everyday.tool_result", "call_id": "one", "result": {"ok": True, "volume": 30}}]
    session._closed.set()
    session.send_everyday_result("late", {})
    assert len(sent) == 1


def test_client_plain_http_remains_available_without_local_executor(monkeypatch):
    client = HermesBridgeClient(AppConfig())
    sent = []
    client._client = SimpleNamespace(
        post=lambda *a, **k: (
            sent.append((a, k))
            or SimpleNamespace(json=lambda: {"text": "plain answer"}, is_success=True, status_code=200)
        )
    )
    assert client.everyday_turn("question", context=context()) == "plain answer"
    assert sent[0][0][0].endswith("/v1/everyday/turn")
    assert everyday_url("http://example/path") == "ws://example/path/v1/everyday/session"
