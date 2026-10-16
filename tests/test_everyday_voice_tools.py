"""Exercise real Live/Realtime event loops with silent hardware/provider transports."""

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from homebody.config import AppConfig
from homebody.realtime_client import RealtimeEvent
from homebody.runtime import HermesVoiceRuntime


def make_runtime():
    config = AppConfig(everyday_camera_enabled=True)
    r = HermesVoiceRuntime(SimpleNamespace(media=SimpleNamespace()), threading.Event(), config_loader=lambda: config)
    r._control_ready.set()
    r._power_mode = "awake"
    r._motors_enabled = True
    r._accept_wake_turn()
    r._play_asset = lambda *_: None
    r._discard_audio = lambda *_: None
    r._clear_streamed_audio = lambda: None
    r._speaker = SimpleNamespace(read=lambda: {"volume": 24, "verified": True})
    return r, config


class Session:
    def __init__(self, runtime, engine, name="get_reachy_volume", mutate_start=False):
        self.runtime, self.engine, self.name = runtime, engine, name
        self.started = time.monotonic()
        self.emitted = False
        self.closed = False
        self.results = []
        self.audio_frames = 0
        self.mutate_start = mutate_start

    def start(self):
        if self.mutate_start:
            self.runtime._request_conversation_stop()
            self.runtime._accept_wake_turn()

    def wait_started(self):
        return []

    def send_audio(self, _):
        self.audio_frames += 1

    def send_tool_result(self, identifier, result, **_):
        self.results.append((identifier, result))

    def send_everyday_result(self, identifier, result):
        self.results.append((identifier, result))

    def events(self):
        if self.results or time.monotonic() - self.started > 2:
            self.runtime._request_conversation_stop()
            return []
        if self.emitted:
            return []
        self.emitted = True
        args = {"purpose": "Read this label"} if self.name == "capture_reachy_camera" else {}
        if self.engine == "live":
            event = RealtimeEvent(
                "everyday.tool_call",
                {"type": "everyday.tool_call", "call_id": "tool-one", "name": self.name, "arguments": args},
            )
        else:
            event = RealtimeEvent(
                "response.output_item.done",
                {
                    "item": {
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "tool-one",
                        "name": self.name,
                        "arguments": json.dumps(args),
                    }
                },
            )
        return [event, event] if self.engine == "realtime" else [event]

    def close(self):
        self.closed = True


@pytest.mark.parametrize("engine", ["live", "realtime"])
def test_each_engine_executes_same_verified_local_tool_once(engine):
    r, config = make_runtime()
    session = Session(r, engine)
    setattr(r, f"_new_{engine}_session", lambda *a, **k: session)

    def read():
        time.sleep(0.005)
        return np.zeros(160, dtype=np.float32)

    r._read_16k_frame = read
    getattr(r, f"_run_{engine}_conversation")(config)
    assert session.closed and session.audio_frames
    assert session.results == [("tool-one", {"ok": True, "volume": 24, "verified": True})]


@pytest.mark.parametrize("engine", ["live", "realtime"])
def test_connection_delay_cannot_donate_new_wake_authority(engine):
    r, config = make_runtime()
    session = Session(r, engine, mutate_start=True)
    setattr(r, f"_new_{engine}_session", lambda *a, **k: session)

    def read():
        time.sleep(0.005)
        return np.zeros(160, dtype=np.float32)

    r._read_16k_frame = read
    r._speaker = SimpleNamespace(read=lambda: pytest.fail("old session must not use new wake"))
    getattr(r, f"_run_{engine}_conversation")(config)
    assert session.closed
    assert session.results and session.results[0][1]["ok"] is False


def test_slow_camera_tool_does_not_block_live_microphone_and_stop_cancels_upload():
    r, config = make_runtime()
    config.camera_enabled = True
    session = Session(r, "live", name="capture_reachy_camera")
    r._new_live_session = lambda *a, **k: session
    entered, release = threading.Event(), threading.Event()

    def capture(**kw):
        entered.set()
        assert release.wait(2)
        return b"jpeg"

    r._capture_camera_jpeg = capture
    r._everyday_describe_frame = lambda *a, **k: pytest.fail("no upload after Stop")

    def read():
        time.sleep(0.005)
        if entered.is_set() and session.audio_frames >= 4:
            r._request_conversation_stop()
            release.set()
        return np.zeros(160, dtype=np.float32)

    r._read_16k_frame = read
    r._run_live_conversation(config)
    assert entered.is_set() and session.audio_frames >= 4 and session.closed
    assert not session.results  # no stale result delivered after Stop
