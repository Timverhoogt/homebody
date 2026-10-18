"""Everyday local actions exercise real runtime guards with silent fake hardware."""

import threading
from types import SimpleNamespace

import pytest

from homebody.config import AppConfig
from homebody.runtime import HermesVoiceRuntime


def runtime(**settings):
    config = AppConfig(**settings)
    value = HermesVoiceRuntime(
        SimpleNamespace(media=SimpleNamespace()), threading.Event(), config_loader=lambda: config
    )
    value._control_ready.set()
    value._power_mode = "awake"
    value._motors_enabled = True
    value._accept_wake_turn()
    value._speaker = SimpleNamespace(
        read=lambda: {"volume": 30, "muted": False, "verified": True}, set=lambda **args: {**args, "verified": True}
    )
    return value, config


def test_camera_request_permission_defaults_off_and_does_not_follow_feed():
    r, c = runtime(camera_feed_enabled=True)
    assert "start_reachy_camera" not in r.everyday_local_tools()
    assert "capture_reachy_camera" not in r.everyday_local_tools()
    c.camera_enabled = True
    assert {"start_reachy_camera", "capture_reachy_camera"} <= set(r.everyday_local_tools())
    assert c.camera_enabled


def test_camera_window_is_bounded_and_does_not_change_saved_permission(monkeypatch):
    r, c = runtime(camera_enabled=True)
    now = [100.0]
    monkeypatch.setattr("homebody.everyday_runtime.time.monotonic", lambda: now[0])
    captures = []
    r._capture_camera_jpeg = lambda **kw: captures.append(1) or b"jpeg"
    result = r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": 30})
    assert result["ok"] and result["camera_session_active"]
    assert len(captures) == 1
    assert c.camera_enabled and not c.camera_feed_enabled and not c.face_tracking_enabled
    now[0] += 31
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]


@pytest.mark.parametrize("duration", [True, 4, 121, 5.5, "30"])
def test_camera_invalid_duration_never_captures(duration):
    r, _ = runtime(camera_enabled=True)
    r._capture_camera_jpeg = lambda **kw: pytest.fail("no capture")
    assert not r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": duration})["ok"]


def test_stop_revokes_session_and_original_fence_cannot_restart():
    r, _ = runtime(camera_enabled=True)
    r._capture_camera_jpeg = lambda **kw: b"jpeg"
    fence = r.everyday_tool_fence()
    assert r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": 30}, fence=fence)["ok"]
    r._request_conversation_stop()
    r._accept_wake_turn()
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]
    assert not r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": 30}, fence=fence)["ok"]


@pytest.mark.parametrize("block", ["kids", "locked", "privacy", "sleep", "workspace", "not_ready"])
def test_runtime_admission_not_just_schema(block):
    r, _ = runtime(camera_enabled=True)
    if block == "kids":
        r._kids_active = True
    elif block == "locked":
        r._kids_locked = True
    elif block == "privacy":
        r._privacy_requested.set()
    elif block == "sleep":
        r._power_mode = "sleep"
    elif block == "workspace":
        r._capability_profile = "agent"
    else:
        r._control_ready.clear()
    assert not r.execute_everyday_tool("set_reachy_volume", {"volume": 30})["ok"]


def test_volume_strictness_replay_and_readback():
    r, _ = runtime()
    fence = r.everyday_tool_fence()
    assert r.execute_everyday_tool("set_reachy_volume", {"volume": 20}, fence=fence, call_id="one")["verified"]
    assert not r.execute_everyday_tool("set_reachy_volume", {"volume": 20}, fence=fence, call_id="one")["ok"]
    for bad in (
        {},
        {"volume": True},
        {"volume": 1.5},
        {"volume": 200},
        {"muted": "yes"},
        {"volume": 10, "muted": False},
        {"volume": 10, "other": 2},
    ):
        assert not r.execute_everyday_tool("set_reachy_volume", bad)["ok"]
    assert r.execute_everyday_tool("get_reachy_volume", {})["volume"] == 30


def test_camera_held_result_dropped_before_vision_after_stop():
    r, _ = runtime(camera_enabled=True)
    entered, release = threading.Event(), threading.Event()

    def capture(**kw):
        entered.set()
        assert release.wait(2)
        return b"jpeg"

    r._capture_camera_jpeg = capture
    r._everyday_describe_frame = lambda *a, **k: pytest.fail("no vision upload after Stop")
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            r.execute_everyday_tool("capture_reachy_camera", {"purpose": "read label"}, fence=r.everyday_tool_fence())
        )
    )
    thread.start()
    assert entered.wait(2)
    r._request_conversation_stop()
    release.set()
    thread.join(2)
    assert not thread.is_alive() and results[0]["ok"] is False


def test_status_is_categorical_not_entire_runtime_or_private_history():
    r, _ = runtime()
    r._status.transcript = "private request"
    result = r.execute_everyday_tool("get_reachy_status", {})
    assert result["power_mode"] == "awake" and result["microphone_recording"] is False
    assert "private" not in str(result) and "transcript" not in result


def test_camera_off_wins_while_an_explicit_look_is_capturing():
    r, _ = runtime(camera_enabled=True)
    entered, release = threading.Event(), threading.Event()

    def capture(**kw):
        entered.set()
        assert release.wait(2)
        return b"jpeg"

    r._capture_camera_jpeg = capture
    r._everyday_describe_frame = lambda *a, **k: pytest.fail("no upload after camera off")
    result = []
    thread = threading.Thread(
        target=lambda: result.append(r.execute_everyday_tool("capture_reachy_camera", {"purpose": "look"}))
    )
    thread.start()
    assert entered.wait(2)
    assert r.execute_everyday_tool("stop_reachy_camera", {})["ok"]
    release.set()
    thread.join(2)
    assert not thread.is_alive() and result[0]["ok"] is False


def test_fresh_explicit_looks_do_not_require_or_reopen_a_camera_window(monkeypatch):
    r, _ = runtime(camera_enabled=True)
    now = [100.0]
    monkeypatch.setattr("homebody.everyday_runtime.time.monotonic", lambda: now[0])
    captures = []
    r._capture_camera_jpeg = lambda **kw: captures.append(1) or b"jpeg"
    r._everyday_describe_frame = lambda *a, **k: "fixture"

    def look():
        return r.execute_everyday_tool("capture_reachy_camera", {"purpose": "read"})

    assert look()["ok"] is True
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]
    assert r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": 5})["ok"]
    assert look()["ok"] is True
    now[0] += 6
    assert look()["ok"] is True  # Fresh request, not continuation of the expired window.
    assert r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": 5})["ok"]
    assert r.execute_everyday_tool("stop_reachy_camera", {})["ok"]
    assert look()["ok"] is True  # Stop cancels old work, not future explicitly authorized looks.
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]
    assert len(captures) == 6


def test_power_uses_existing_handler_and_returns_actual_confirmation():
    r, _ = runtime()
    calls = []

    def power(mode, **kwargs):
        calls.append((mode, kwargs))
        return {"power_mode": mode}

    r.set_power_mode = power
    assert r.execute_everyday_tool("set_reachy_power_mode", {"mode": "standby"}) == {
        "ok": True,
        "verified": True,
        "mode": "standby",
        "conversation_ended": True,
    }
    assert r.execute_everyday_tool("set_reachy_power_mode", {"mode": "meeting", "duration_minutes": 20})["ok"]
    assert calls[-1] == ("meeting", {"duration_seconds": 1200})
    r.set_power_mode = lambda *a, **k: {"power_mode": "awake"}
    assert r.execute_everyday_tool("set_reachy_power_mode", {"mode": "sleep"})["ok"] is False


def test_stopped_waiter_cannot_change_volume_after_motor_lock_is_released():
    r, _ = runtime()
    admitted = threading.Event()
    r._speaker = SimpleNamespace(set=lambda **kw: pytest.fail("late mutation"))
    fence = r.everyday_tool_fence()
    result = []
    with r._motor_transition_lock:

        def change():
            admitted.set()
            result.append(r.execute_everyday_tool("set_reachy_volume", {"volume": 20}, fence=fence))

        thread = threading.Thread(target=change)
        thread.start()
        assert admitted.wait(2)
        r._request_conversation_stop()
    thread.join(2)
    assert not thread.is_alive() and result[0]["ok"] is False
