from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from homebody.config import AppConfig
from homebody.realtime_client import RealtimeBridgeError, RealtimeBridgeSession, RealtimeEvent, realtime_url


def test_realtime_url_uses_private_bridge() -> None:
    assert realtime_url("http://192.168.1.10:8643") == "ws://192.168.1.10:8643/v1/realtime"
    assert realtime_url("https://voice.example") == "wss://voice.example/v1/realtime"


def test_audio_delta_decodes_pcm16() -> None:
    expected = np.array([-1.0, 0.0, 0.5], dtype=np.float32)
    pcm = (expected * 32767).astype("<i2").tobytes()
    event = RealtimeEvent(
        "response.output_audio.delta",
        {"delta": base64.b64encode(pcm).decode("ascii")},
    )
    actual = RealtimeBridgeSession.audio_samples(event)
    assert np.allclose(actual, expected, atol=1e-4)


def test_truncate_audio_reports_played_offset() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str) -> None:
            self.messages.append(message)

    session = RealtimeBridgeSession.__new__(RealtimeBridgeSession)
    socket = Socket()
    session._socket = socket  # type: ignore[assignment]

    session.truncate_audio("item-123", 1450)

    assert json.loads(socket.messages[0]) == {
        "type": "conversation.item.truncate",
        "item_id": "item-123",
        "content_index": 0,
        "audio_end_ms": 1450,
    }


def test_camera_frame_creates_image_and_tool_output() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str) -> None:
            self.messages.append(message)

    session = RealtimeBridgeSession.__new__(RealtimeBridgeSession)
    socket = Socket()
    session._socket = socket  # type: ignore[assignment]

    session.send_camera_frame("camera-call", b"jpeg-data")

    events = [json.loads(message) for message in socket.messages]
    assert events[0]["item"]["type"] == "function_call_output"
    assert events[0]["item"]["call_id"] == "camera-call"
    image_part = events[1]["item"]["content"][1]
    assert image_part["type"] == "input_image"
    assert image_part["detail"] == "high"
    assert base64.b64decode(image_part["image_url"].split(",", 1)[1]) == b"jpeg-data"
    assert events[2] == {"type": "response.create"}


def test_robot_tool_result_completes_call_and_continues_response() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str) -> None:
            self.messages.append(message)

    session = RealtimeBridgeSession.__new__(RealtimeBridgeSession)
    socket = Socket()
    session._socket = socket  # type: ignore[assignment]

    session.send_tool_result("robot-call", {"ok": True, "queued": "look_left"})

    events = [json.loads(message) for message in socket.messages]
    assert events[0]["item"] == {
        "type": "function_call_output",
        "call_id": "robot-call",
        "output": '{"ok": true, "queued": "look_left"}',
    }
    assert events[1] == {"type": "response.create"}


def test_power_tool_result_can_end_session_without_requesting_more_speech() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str) -> None:
            self.messages.append(message)

    session = RealtimeBridgeSession.__new__(RealtimeBridgeSession)
    socket = Socket()
    session._socket = socket  # type: ignore[assignment]

    session.send_tool_result(
        "power-call",
        {"ok": True, "mode": "sleep"},
        continue_response=False,
    )

    events = [json.loads(message) for message in socket.messages]
    assert events == [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "power-call",
                "output": '{"ok": true, "mode": "sleep"}',
            },
        }
    ]


def _receiver_session(messages: list[object], *, closing_error: Exception | None = None) -> RealtimeBridgeSession:
    """Build a session whose socket yields messages, then fails like a dropped connection."""

    class Socket:
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield from messages
            if closing_error is not None:
                raise closing_error

    session = RealtimeBridgeSession(AppConfig())
    session._socket = Socket()  # type: ignore[assignment]
    return session


def test_realtime_url_keeps_reverse_proxy_path_prefix() -> None:
    assert realtime_url("https://host.example/hermes") == "wss://host.example/hermes/v1/realtime"
    assert realtime_url("http://host.example/hermes/") == "ws://host.example/hermes/v1/realtime"


def test_receive_loop_skips_malformed_messages_and_reports_disconnect() -> None:
    session = _receiver_session(
        ["not json", "[1, 2]", json.dumps({"type": "response.done"})],
        closing_error=RuntimeError("socket dropped"),
    )

    session._receive_loop()

    events = session.events()
    assert [event.type for event in events] == ["response.done", "bridge.error"]
    assert "socket dropped" in events[-1].payload["error"]


def test_full_queue_sheds_audio_and_keeps_control_and_error_events() -> None:
    audio = json.dumps({"type": "response.output_audio.delta", "delta": ""})
    session = _receiver_session(
        [audio] * 512 + [json.dumps({"type": "response.function_call_arguments.done"})],
        closing_error=RuntimeError("socket dropped"),
    )

    session._receive_loop()

    types = [event.type for event in session.events()]
    assert len(types) == 512
    assert types[-2:] == ["response.function_call_arguments.done", "bridge.error"]


def test_clear_output_wraps_socket_failures() -> None:
    class Socket:
        def send(self, _message: str) -> None:
            raise OSError("connection reset")

    session = RealtimeBridgeSession(AppConfig())
    session._socket = Socket()  # type: ignore[assignment]

    with pytest.raises(RealtimeBridgeError, match="clear Realtime output"):
        session.clear_output()
