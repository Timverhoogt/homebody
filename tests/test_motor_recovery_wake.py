"""Fresh-wake integration with real runtime guards; no physical/network operations."""

from types import SimpleNamespace

import httpx
import pytest
from test_everyday_runtime import runtime

from homebody.motor_recovery import MotorRecoveryError, replace_control_client
from homebody.power import MotorBackendUnavailable, wake_with_backend_recovery

POSE = dict(x=0.0, y=0.0, z=-0.04, roll=0.0, pitch=0.3, yaw=0.0)


def prepared(monkeypatch, *, daemon="error"):
    r, _ = runtime(api_key="", household_api_key="")
    events = []
    r._motion = SimpleNamespace(fence_motor_recovery=lambda: events.append("motion-fenced"))
    r.robot.client = SimpleNamespace(is_connected=lambda: False)

    def get(url, **kw):
        events.append(url.rsplit("/", 1)[-1])
        if url.endswith("/daemon/status"):
            body = {"state": daemon, "backend_status": None if daemon == "error" else {}}
        else:
            body = {"control_mode": "disabled", "head_pose": POSE.copy()}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr("homebody.power.httpx.get", get)

    def recover(**kw):
        assert kw["current"]()
        events.append("backend-rebuilt-torque-off")

    monkeypatch.setattr("homebody.power.recover_motor_backend", recover)

    def rebind(robot, **kw):
        assert kw["current"]()
        events.append("control-socket-only")
        robot.client = SimpleNamespace(is_connected=lambda: True)

    monkeypatch.setattr("homebody.power.replace_control_client", rebind)
    attempts = []

    def set_mode(enabled, *, wake):
        attempts.append((enabled, wake))
        if daemon == "error" and len(attempts) == 1:
            e = MotorBackendUnavailable("pre-admission503")
            e.wake_recovery_safe = True
            raise e
        r._motors_enabled = True

    r._set_motor_mode = set_mode
    return r, events, attempts


def test_fresh_wake_rebuilds_then_rebinds_and_only_then_admits_normal_wake(monkeypatch):
    r, events, attempts = prepared(monkeypatch)
    old = r.everyday_tool_fence()
    wake_with_backend_recovery(r, allow_recovery=True)
    assert attempts == [(True, True), (True, True)]  # First was a proved refusal, not an admitted write.
    assert (
        events.index("motion-fenced")
        < events.index("backend-rebuilt-torque-off")
        < events.index("control-socket-only")
    )
    assert r._motors_enabled is True
    assert not r.execute_everyday_tool("set_reachy_volume", {"volume": 30}, fence=old)["ok"]
    assert r.execute_everyday_tool("get_reachy_volume", {})["ok"]


@pytest.mark.parametrize("accept_new_wake", [False, True])
def test_stop_during_recovery_cannot_be_rebased_into_a_new_wake(monkeypatch, accept_new_wake):
    r, events, attempts = prepared(monkeypatch)

    def held(**kw):
        r._request_conversation_stop()
        if accept_new_wake:
            r._accept_wake_turn()
        assert not kw["current"]()

    monkeypatch.setattr("homebody.power.recover_motor_backend", held)
    with pytest.raises(MotorRecoveryError, match="cancelled"):
        wake_with_backend_recovery(r, allow_recovery=True)
    assert attempts == [(True, True)]
    assert "control-socket-only" not in events


def test_wrapper_retains_receipt_even_if_recovery_returns_after_new_wake(monkeypatch):
    r, events, attempts = prepared(monkeypatch)

    def late(**kw):
        r._request_conversation_stop()
        r._accept_wake_turn()

    r._recover_motor_backend_for_wake = late
    with pytest.raises(MotorRecoveryError, match="cancelled"):
        wake_with_backend_recovery(r, allow_recovery=True)
    assert len(attempts) == 1


def test_orphaned_completed_recovery_repairs_only_socket_on_next_fresh_wake(monkeypatch):
    r, events, attempts = prepared(monkeypatch, daemon="running")
    monkeypatch.setattr(
        "homebody.power.recover_motor_backend", lambda **kw: pytest.fail("No duplicate backend recovery")
    )
    wake_with_backend_recovery(r, allow_recovery=True)
    assert attempts == [(True, True)] and "control-socket-only" in events


def test_automatic_kids_first_listen_has_no_recovery_authority(monkeypatch):
    r, events, attempts = prepared(monkeypatch)
    with pytest.raises(MotorBackendUnavailable):
        wake_with_backend_recovery(r, allow_recovery=False)
    assert len(attempts) == 1 and not events


@pytest.mark.parametrize("error", [httpx.ReadTimeout("ambiguous"), httpx.ConnectError("offline")])
def test_ambiguous_enable_is_never_repeated_or_recovered(monkeypatch, error):
    r, _ = runtime(api_key="", household_api_key="")
    r._motors_enabled = None
    attempts = []

    def post(*a, **kw):
        attempts.append(a)
        raise error

    monkeypatch.setattr("homebody.power.httpx.post", post)
    monkeypatch.setattr("homebody.power.recover_motor_backend", lambda **kw: pytest.fail("No recovery"))
    with pytest.raises(MotorBackendUnavailable):
        wake_with_backend_recovery(r, allow_recovery=True)
    assert len(attempts) == 1


def test_control_socket_rebind_does_not_construct_robot_or_touch_media():
    calls = []
    old = SimpleNamespace(disconnect=lambda: calls.append("old-disconnected"))
    fresh = SimpleNamespace(
        wait_for_connection=lambda **kw: calls.append("fresh-telemetry"),
        disconnect=lambda: calls.append("fresh-disconnected"),
    )
    robot = SimpleNamespace(client=old, media=object())
    before = robot.media
    replace_control_client(robot, current=lambda: True, connect=lambda: fresh)
    assert robot.client is fresh and robot.media is before
    assert calls == ["old-disconnected", "fresh-telemetry"]


def test_disconnected_client_failure_never_publishes_half_ready_client():
    calls = []
    old = SimpleNamespace(disconnect=lambda: calls.append("old"))

    def timeout(**kw):
        raise TimeoutError()

    fresh = SimpleNamespace(wait_for_connection=timeout, disconnect=lambda: calls.append("fresh"))
    robot = SimpleNamespace(client=old)
    with pytest.raises(MotorRecoveryError):
        replace_control_client(robot, current=lambda: True, connect=lambda: fresh)
    assert robot.client is old and calls == ["old", "fresh"]


@pytest.mark.parametrize("detail,safe", [("Backend not running", True), ("Another failure", False)])
def test_real_motor_mode_marks_only_exact_no_admission_as_recoverable(monkeypatch, detail, safe):
    r, _ = runtime(api_key="", household_api_key="")
    r._motors_enabled = None

    def post(url, **kw):
        return httpx.Response(503, json={"detail": detail}, request=httpx.Request("POST", url))

    monkeypatch.setattr("homebody.power.httpx.post", post)
    with pytest.raises(MotorBackendUnavailable) as error:
        r._set_motor_mode(True, wake=True)
    assert error.value.wake_recovery_safe is safe


def test_repeat_power_loss_after_recovery_is_reported_not_a_dead_wake_thread(monkeypatch):
    r, events, attempts = prepared(monkeypatch)
    original_get = __import__("homebody.power", fromlist=["httpx"]).httpx.get

    def get(url, **kw):
        if url.endswith("/api/state/full"):
            raise httpx.ReadTimeout("lost again")
        return original_get(url, **kw)

    monkeypatch.setattr("homebody.power.httpx.get", get)
    with pytest.raises(MotorRecoveryError):
        wake_with_backend_recovery(r, allow_recovery=True)
    assert len(attempts) == 1 and "control-socket-only" not in events
