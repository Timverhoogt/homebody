"""Live session permission changes and transport closure, without hardware or network."""

import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from test_live_voice import FakeLiveSession
from test_owner_everyday_camera import ready
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from homebody.config import AppConfig
from homebody.live_client import LiveBridgeSession
from homebody.realtime_client import RealtimeBridgeError, RealtimeEvent


def normal_close():
    return ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)


def test_audio_send_normal_remote_close_is_terminal_not_error():
    session = LiveBridgeSession(AppConfig())
    attempts = []

    def send(_payload):
        attempts.append(1)
        raise normal_close()

    session._socket = SimpleNamespace(send=send)
    session.started.set()
    session.send_audio(np.zeros(160))
    session.send_audio(np.zeros(160))
    assert attempts == [1]
    assert session._closed.is_set() and not session.started.is_set()
    assert [event.type for event in session.events()] == ["session.closed"]


def test_receiver_clean_eof_ends_session_before_next_audio():
    session = LiveBridgeSession(AppConfig())
    sent = []

    class Socket:
        def __iter__(self):
            return iter(())

        def send(self, payload):
            sent.append(payload)

    session._socket = Socket()
    session.started.set()
    session._receive_loop()
    session.send_audio(np.zeros(160))
    assert sent == []
    assert [event.type for event in session.events()] == ["session.closed"]
    assert session._closed.is_set() and not session.started.is_set()


def test_abnormal_send_is_still_an_error():
    session = LiveBridgeSession(AppConfig())

    def send(_payload):
        raise ConnectionClosedError(Close(1011, "upstream failed"), None, None)

    session._socket = SimpleNamespace(send=send)
    session.started.set()
    with pytest.raises(RealtimeBridgeError, match="Could not stream microphone audio"):
        session.send_audio(np.zeros(160))


def test_abnormal_receive_keeps_error_before_terminal_event():
    session = LiveBridgeSession(AppConfig())

    class Socket:
        def __iter__(self):
            raise ConnectionClosedError(Close(1011, "upstream failed"), None, None)

    session._socket = Socket()
    session._receive_loop()
    assert [event.type for event in session.events()] == ["bridge.error", "session.closed"]


def runtime_fixture(camera_enabled=False):
    configs = [AppConfig(camera_enabled=camera_enabled, local_vision_enabled=True)]
    runtime, vision = ready(lambda: configs[0])
    session = FakeLiveSession([[RealtimeEvent("session.closed", {})]])
    runtime._new_live_session = lambda *a, **kw: session
    runtime._play_asset = lambda *_: None
    runtime._discard_audio = lambda *_: None
    runtime._motion = None
    runtime._clear_streamed_audio = lambda: session.order.append("clear_audio")
    statuses = []
    set_status = runtime._set_status

    def status(state, detail="", **kw):
        statuses.append((state, detail))
        set_status(state, detail, **kw)

    runtime._set_status = status
    captures = []
    runtime._capture_camera_jpeg = lambda **kw: captures.append(1) or b"synthetic"
    return runtime, configs, session, statuses, captures, vision


@pytest.mark.parametrize("boundary", ["startup", "microphone", "events"])
@pytest.mark.parametrize("transition", ["enable", "disable", "disable_reenable", "camera_stop"])
def test_camera_change_retires_session_without_dispatch_or_listen_restart(boundary, transition):
    runtime, configs, session, statuses, captures, vision = runtime_fixture(transition != "enable")
    dispatched = []
    runtime.launch_everyday_tool = lambda *a, **kw: dispatched.append("tool")
    runtime._start_live_look = lambda *a, **kw: dispatched.append("look")
    reads = []

    def change():
        if transition in {"disable", "disable_reenable", "camera_stop"}:
            configs[0] = replace(configs[0], camera_enabled=False if transition != "camera_stop" else True)
            runtime.revoke_everyday_camera()
        if transition in {"enable", "disable_reenable"}:
            configs[0] = replace(configs[0], camera_enabled=True)

    def read():
        reads.append(1)
        if boundary == "microphone":
            change()
        return np.zeros(160)

    def events():
        if boundary == "events":
            change()
        return [
            RealtimeEvent("bridge.look_request", {"delegation_id": "old", "question": "look at this"}),
            RealtimeEvent("everyday.tool_call", {"call_id": "old", "name": "start_reachy_camera",
                                                   "arguments": {"duration_seconds": 30}}),
            RealtimeEvent("session.closed", {}),
        ]

    runtime._read_16k_frame = read
    session.events = events
    if boundary == "startup":
        session.wait_started = lambda: change() or []
    with runtime._voice_activity_lock:
        runtime._run_live_conversation(configs[0])
    assert dispatched == [] and captures == [] and vision == []
    assert session.order == ["start", "clear_audio", "close"]
    assert len(reads) == (0 if boundary == "startup" else 1)
    assert session.sent == (1 if boundary == "events" else 0)
    assert any("wake" in detail.lower() and "camera" in detail.lower() for _, detail in statuses)
    assert not runtime.everyday_local_capabilities()["activity"]["camera_session_active"]
    runtime._set_status("waiting_for_wake_word", "Local wake detection only")
    assert "repeat your request" in runtime._status.last_error
    assert runtime._turn_error[0] == runtime._status.last_error


def test_fresh_wake_after_enable_has_fresh_authority_and_tool_list(monkeypatch):
    runtime, configs, old_session, _, captures, vision = runtime_fixture(False)
    original = runtime.everyday_tool_fence(threading.Event())
    with runtime._voice_activity_lock:
        old_authority = runtime._companion_authority("voice")
        configs[0] = replace(configs[0], camera_enabled=True)
        assert not old_authority()
        observed = []
        runtime._live_loop = lambda *a, **kw: observed.append(kw["look_authority"]())
        runtime._run_live_conversation(configs[0])
        assert observed == [True]
    assert not original.cancelled.is_set()  # no mutation/reuse of the old request fence
    assert captures == [] and vision == []

    sent = []

    class Socket:
        def send(self, payload):
            sent.append(json.loads(payload))

        def __iter__(self):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr("homebody.live_client.connect", lambda *a, **kw: Socket())
    session = LiveBridgeSession(configs[0])
    try:
        session.start()
    finally:
        session.close()
    assert {"capture_reachy_camera", "start_reachy_camera"} <= set(sent[0]["local_tools"])


@pytest.mark.parametrize("boundary", ["startup", "microphone"])
@pytest.mark.parametrize("cancel", ["stop", "privacy", "shutdown"])
def test_stop_privacy_win_over_camera_restart_guidance(boundary, cancel):
    runtime, configs, session, statuses, captures, vision = runtime_fixture(False)

    def change():
        configs[0] = replace(configs[0], camera_enabled=True)
        if cancel == "stop":
            runtime._request_conversation_stop()
        elif cancel == "privacy":
            runtime._privacy_requested.set()
        else:
            runtime.stop_event.set()

    def read():
        change()
        return np.zeros(160)

    runtime._read_16k_frame = read
    if boundary == "startup":
        session.wait_started = lambda: change() or []
    with runtime._voice_activity_lock:
        runtime._run_live_conversation(configs[0])
    assert not captures and not vision and session.sent == 0
    assert not any("Camera access changed" in detail for _, detail in statuses)
    assert session.order[-2:] == ["clear_audio", "close"]


def test_config_object_mutation_does_not_refresh_session_permission():
    runtime, configs, session, statuses, captures, vision = runtime_fixture(False)

    def read():
        configs[0].camera_enabled = True
        return np.zeros(160)

    runtime._read_16k_frame = read
    with runtime._voice_activity_lock:
        runtime._run_live_conversation(configs[0])
    assert session.sent == 0 and not captures and not vision
    assert any("Camera access changed" in detail for _, detail in statuses)


def test_owner_disable_reenable_retires_live_and_cancels_original_fence(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from test_owner_auth import ORIGIN, pair

    import homebody.main as main

    runtime, configs, session, statuses, captures, vision = runtime_fixture(True)
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: configs[0])

    def save(config):
        configs[0] = config
        return tmp_path / "not-written.json"

    monkeypatch.setattr(main, "save_config", save)
    app = main.Homebody()
    app._runtime = runtime
    app._agent_listener = SimpleNamespace(sync=lambda _: None)
    fences = []
    original = runtime.everyday_tool_fence

    def fence(cancelled=None):
        result = original(cancelled)
        if cancelled is not None:
            fences.append(result)
        return result

    runtime.everyday_tool_fence = fence
    client = TestClient(app.settings_app, base_url=ORIGIN)
    try:
        code = app.settings_app.state.owner_store.provision(ORIGIN)
        assert pair(client, code).status_code == 200
        headers = {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}

        def read():
            for enabled in (False, True):
                response = client.post("/api/settings", json={"camera_enabled": enabled}, headers=headers)
                assert response.status_code == 200
            return np.zeros(160)

        runtime._read_16k_frame = read
        with runtime._voice_activity_lock:
            runtime._run_live_conversation(configs[0])
    finally:
        client.close()
    assert len(fences) == 1 and fences[0].cancelled.is_set()
    assert session.sent == 0 and not captures and not vision
    assert configs[0].camera_enabled
    assert session.order[-2:] == ["clear_audio", "close"]
    assert any("Camera access changed" in detail for _, detail in statuses)
    assert not (tmp_path / "not-written.json").exists()


def test_restart_guidance_expires_without_permanently_suppressing_offers(monkeypatch):
    runtime, configs, session, _, _, _ = runtime_fixture(False)
    session.wait_started = lambda: setattr(configs[0], "camera_enabled", True) or []
    with runtime._voice_activity_lock:
        runtime._run_live_conversation(configs[0])
    assert runtime._status.last_error
    _, deadline = runtime._turn_error
    monkeypatch.setattr("homebody.runtime.time.monotonic", lambda: deadline + 1)
    runtime._expire_turn_error()
    assert runtime._turn_error is None and runtime._status.last_error == ""
