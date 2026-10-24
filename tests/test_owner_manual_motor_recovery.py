"""Dashboard wake must use recovery, with original Owner/Stop authority intact."""

import threading

import pytest
import test_owner_routes
from test_motor_recovery_wake import prepared
from test_owner_auth import ORIGIN, pair

application = test_owner_routes.application


@pytest.mark.parametrize("previous_mode", ["standby", "sleep", "meeting"])
def test_owner_dashboard_awake_recovers_missing_backend_with_its_own_receipt(application, monkeypatch, previous_mode):
    app, client, code = application
    runtime, events, attempts = prepared(monkeypatch)
    runtime._control_ready.set()
    runtime._motion.awake = lambda: None
    runtime.robot.media.start_recording = lambda: None
    runtime._power_mode = previous_mode
    if previous_mode in {"sleep", "meeting"}:
        runtime._privacy_requested.set()
    runtime._conversation_stop_generation = 7
    runtime._turn_stop_generation = 1  # A stale voice receipt must not be borrowed or overwritten.
    app._runtime = runtime
    assert pair(client, code).status_code == 200
    csrf = client.get("/api/owner/session").json()["csrf"]
    response = client.post("/api/power", json={"mode": "awake"}, headers={"Origin": ORIGIN, "X-Homebody-CSRF": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert attempts == [(True, True), (True, True)]
    assert "backend-rebuilt-torque-off" in events and "control-socket-only" in events
    assert runtime._turn_stop_generation == 1


def test_automatic_power_caller_does_not_gain_recovery_authority(monkeypatch):
    runtime, events, attempts = prepared(monkeypatch)
    with pytest.raises(RuntimeError):
        runtime.set_power_mode("awake")
    assert attempts == [(True, True)]
    assert "backend-rebuilt-torque-off" not in events


@pytest.mark.parametrize("new_wake", [False, True])
def test_stop_while_dashboard_request_waits_for_motor_lock_wins(monkeypatch, new_wake):
    runtime, events, attempts = prepared(monkeypatch)
    entered = threading.Event()
    original = runtime.revoke_camera_control

    def revoke():
        entered.set()
        original()

    runtime.revoke_camera_control = revoke
    errors = []

    def request():
        try:
            runtime.set_power_mode("awake", allow_motor_recovery=True)
        except RuntimeError as error:
            errors.append(error)

    runtime._motor_transition_lock.acquire()
    worker = threading.Thread(target=request)
    try:
        worker.start()
        assert entered.wait(1)
        runtime._request_conversation_stop()
        if new_wake:
            runtime._accept_wake_turn()
    finally:
        runtime._motor_transition_lock.release()
        worker.join(2)
    assert not worker.is_alive()
    assert errors and not attempts and "backend-rebuilt-torque-off" not in events
    if not new_wake:
        assert runtime._conversation_stop_requested.is_set()


@pytest.mark.parametrize("boundary", ["recovery", "socket"])
@pytest.mark.parametrize("cancellation", ["stop", "new_wake", "privacy"])
def test_cancelled_dashboard_recovery_never_falls_back_to_fold(monkeypatch, boundary, cancellation):
    runtime, events, attempts = prepared(monkeypatch)
    runtime._motion.awake = lambda: None

    def cancelled(**kwargs):
        runtime._request_conversation_stop()
        if cancellation == "new_wake":
            runtime._accept_wake_turn()
        if cancellation == "privacy":
            runtime._privacy_requested.set()

    if boundary == "recovery":
        monkeypatch.setattr("homebody.power.recover_motor_backend", cancelled)
    else:
        monkeypatch.setattr("homebody.power.replace_control_client", lambda robot, **kwargs: cancelled(**kwargs))
    runtime._fold_head_before_torque_release = lambda: pytest.fail("Cancelled original wake must not fold")
    with pytest.raises(RuntimeError):
        runtime.set_power_mode("awake", allow_motor_recovery=True)
    assert attempts == [(True, True)]
