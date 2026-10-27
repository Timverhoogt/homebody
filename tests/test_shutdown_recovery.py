"""Shutdown recovery uses real runtime guards with synthetic hardware only."""
import threading
from types import SimpleNamespace

import httpx
import pytest
from test_everyday_runtime import runtime

from homebody import shutdown_runtime

POSE = dict(x=-.021, y=.001, z=-.044, roll=0., pitch=.426, yaw=0.)
STATE = {"control_mode": "disabled", "head_pose": POSE}


def prepared(monkeypatch, *, absent=True, state=None):
    r, _ = runtime(api_key="", household_api_key="")
    events = []
    r._runtime_started = True
    r.robot.media = SimpleNamespace(
        stop_playing=lambda: events.append("playback-stopped"),
        stop_recording=lambda: events.append("recording-stopped"),
    )
    r._recording = True
    r._motion = SimpleNamespace(fence_motor_recovery=lambda: events.append("motion-fenced"))
    r._clear_streamed_audio = lambda: events.append("audio-cleared")
    r._end_face_follow = lambda: events.append("tracking-ended")
    r._actions = SimpleNamespace(
        fence_reason="", fence=lambda reason: events.append("fenced"),
        unfence=lambda: events.append("unfenced"),
        cancel=lambda **kw: events.append("actions-cancelled"),
        wait_idle=lambda **kw: True,
    )
    reads = [0]

    def read():
        reads[0] += 1
        events.append("measured")
        if absent and reads[0] == 1:
            request = httpx.Request("GET", "http://127.0.0.1:8000/api/state/full")
            response = httpx.Response(503, json={"detail": "Backend not running"}, request=request)
            response.raise_for_status()
        return STATE.copy() if state is None else state

    monkeypatch.setattr(shutdown_runtime, "_read_state", read)

    def recover(**kw):
        assert kw["current"]()
        events.append("reconnected-torque-off")
        return STATE

    monkeypatch.setattr(shutdown_runtime, "recover_motor_backend", recover)
    r._set_motor_mode = lambda *a, **kw: pytest.fail("No torque write in already-folded shutdown")
    r._run_native_sleep = lambda: pytest.fail("No fold in already-folded shutdown")

    def schedule():
        events.append("scheduled")
        assert r._motor_transition_lock._is_owned()
        return {"protocol": 1, "state": "scheduled", "delay_seconds": 5}

    return r, events, schedule


@pytest.mark.parametrize("absent", [False, True])
def test_folded_shutdown_never_enables_torque_or_wakes(monkeypatch, absent):
    r, events, schedule = prepared(monkeypatch, absent=absent)
    assert shutdown_runtime.request_host_shutdown(r, schedule)["state"] == "scheduled"
    assert ("reconnected-torque-off" in events) == absent
    assert events.index("playback-stopped") < events.index("scheduled")
    assert events.index("actions-cancelled") < events.index("scheduled")
    assert r._power_mode == "sleep" and r._privacy_requested.is_set()
    assert r._motors_enabled is False and r._head_safely_folded
    assert r._pending_privacy_transitions == 0


@pytest.mark.parametrize("error", [httpx.ReadTimeout("ambiguous"), httpx.ConnectError("offline")])
def test_ambiguous_state_failure_is_not_recovered(monkeypatch, error):
    r, events, schedule = prepared(monkeypatch)
    def fail():
        raise error
    monkeypatch.setattr(shutdown_runtime, "_read_state", fail)
    with pytest.raises(RuntimeError):
        shutdown_runtime.request_host_shutdown(r, schedule)
    assert "reconnected-torque-off" not in events and "scheduled" not in events


@pytest.mark.parametrize("stage", ["drain", "reconnect", "readback"])
def test_stop_cancels_original_shutdown_even_after_new_wake(monkeypatch, stage):
    r, events, schedule = prepared(monkeypatch)
    def cancel():
        r._request_conversation_stop()
        r._accept_wake_turn()
    if stage == "drain":
        r._actions.wait_idle = lambda **kw: cancel() or True
    elif stage == "reconnect":
        def recover(**kw):
            cancel()
            return STATE
        monkeypatch.setattr(shutdown_runtime, "recover_motor_backend", recover)
    else:
        original = shutdown_runtime._read_state
        def read():
            result = original()
            cancel()
            return result
        monkeypatch.setattr(shutdown_runtime, "_read_state", read)
    with pytest.raises(RuntimeError, match="cancelled"):
        shutdown_runtime.request_host_shutdown(r, schedule)
    assert "scheduled" not in events


def test_stop_while_waiting_for_motor_lock_cancels_without_recovery(monkeypatch):
    r, events, schedule = prepared(monkeypatch)
    admitted = threading.Event()
    original = r._request_conversation_stop
    def stop():
        receipt = original()
        admitted.set()
        return receipt
    r._request_conversation_stop = stop
    errors = []
    def run():
        try:
            shutdown_runtime.request_host_shutdown(r, schedule)
        except RuntimeError as exc:
            errors.append(str(exc))
    with r._motor_transition_lock:
        thread = threading.Thread(target=run)
        thread.start()
        assert admitted.wait(2)
        original()
    thread.join(3)
    assert not thread.is_alive() and errors and "cancelled" in errors[0]
    assert "scheduled" not in events and "reconnected-torque-off" not in events


@pytest.mark.parametrize("state", [
    {"control_mode": "enabled", "head_pose": POSE},
    {"control_mode": "disabled", "head_pose": {**POSE, "z": 0.}},
    {"control_mode": "disabled", "head_pose": {**POSE, "pitch": float("nan")}},
])
def test_recovered_unsafe_state_never_initiates_folding(monkeypatch, state):
    r, events, schedule = prepared(monkeypatch, state=state)
    with pytest.raises(RuntimeError, match="folded"):
        shutdown_runtime.request_host_shutdown(r, schedule)
    assert "scheduled" not in events
    r._reconcile_power_mode()  # A refused recovery cannot trigger a later automatic fold.
    assert "scheduled" not in events


def test_no_recovery_when_native_sleep_is_uncertain(monkeypatch):
    r, events, schedule = prepared(monkeypatch)
    r._native_sleep_uncertain = True
    with pytest.raises(RuntimeError):
        shutdown_runtime.request_host_shutdown(r, schedule)
    assert "reconnected-torque-off" not in events and "scheduled" not in events


def test_failed_drain_refuses_reconstruction(monkeypatch):
    r, events, schedule = prepared(monkeypatch)
    r._actions.wait_idle = lambda **kw: False
    with pytest.raises(RuntimeError, match="drain"):
        shutdown_runtime.request_host_shutdown(r, schedule)
    assert "reconnected-torque-off" not in events and "scheduled" not in events


def test_healthy_upright_retains_normal_sleep_then_fresh_guard(monkeypatch):
    upright = {"control_mode": "enabled", "head_pose": {**POSE, "z": 0., "pitch": 0.}}
    r, events, schedule = prepared(monkeypatch, absent=False, state=upright)
    current = [upright]
    monkeypatch.setattr(shutdown_runtime, "_read_state", lambda: current[0])
    def fold():
        events.append("normal-sleep")
        current[0] = STATE
        r._set_status("sleep", "Folded", last_error="", power_mode="sleep")
    r._apply_power_mode = fold
    assert shutdown_runtime.request_host_shutdown(r, schedule)["state"] == "scheduled"
    assert events.index("normal-sleep") < events.index("scheduled")
    assert "reconnected-torque-off" not in events
