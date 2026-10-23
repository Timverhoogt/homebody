"""HTTP contract fixtures only; never opens hardware, camera, audio or a real socket."""

import uuid
from types import SimpleNamespace

import httpx
import pytest

from homebody.motor_recovery import MotorRecoveryError, backend_admission_rejected, recover_motor_backend

JOB = str(uuid.UUID(int=1))
POSE = dict(x=0.0, y=0.0, z=-0.04, roll=0.0, pitch=0.3, yaw=0.0)


def fixture():
    state = SimpleNamespace(
        now=0.0,
        valid=True,
        calls=[],
        status_reads=0,
        jobs=0,
        hook=lambda path: None,
        job_status="done",
        torque="disabled",
        cap=True,
        before="error",
    )

    def respond(request):
        path = request.url.path
        state.calls.append((request.method, path))
        if path == "/api/daemon/status":
            state.status_reads += 1
            data = (
                {"state": state.before, "backend_status": None}
                if state.status_reads == 1
                else {"state": "running", "backend_status": {}, "error": None}
            )
        elif path == "/api/daemon/motor-recovery":
            data = {"schema": 1, "supported": state.cap, "torque_off_only": True}
        elif path == "/api/daemon/reconnect-motors":
            data = {"job_id": JOB}
        elif path.startswith("/api/apps/job-status/"):
            state.jobs += 1
            data = {"command": "daemon-reconnect-motors", "status": state.job_status, "logs": ["PRIVATE_NATIVE_LOG"]}
        elif path == "/api/state/full":
            data = {"control_mode": state.torque, "head_pose": POSE.copy()}
        else:
            pytest.fail(path)
        state.hook(path)
        return httpx.Response(200, json=data)

    state.client = httpx.Client(transport=httpx.MockTransport(respond))
    state.wait = lambda n: setattr(state, "now", state.now + n)
    state.run = lambda: recover_motor_backend(
        client=state.client, current=lambda: state.valid, clock=lambda: state.now, wait=state.wait, timeout=0.35
    )
    return state


def test_exact_job_then_healthy_torque_off_readback_without_wake_or_restart():
    s = fixture()
    assert s.run()["head_pose"] == POSE
    assert [p for m, p in s.calls if m == "POST"] == ["/api/daemon/reconnect-motors"]
    assert s.calls[-1] == ("GET", "/api/state/full")


@pytest.mark.parametrize(
    "path",
    [
        "/api/daemon/status",
        "/api/daemon/motor-recovery",
        "/api/daemon/reconnect-motors",
        "/api/apps/job-status/" + JOB,
        "/api/state/full",
    ],
)
def test_stop_at_every_await_boundary_prevents_further_operations(path):
    s = fixture()
    s.hook = lambda p: setattr(s, "valid", False) if p == path else None
    with pytest.raises(MotorRecoveryError, match="cancelled"):
        s.run()
    assert s.calls[-1][1] == path
    assert sum(m == "POST" for m, p in s.calls) <= 1


@pytest.mark.parametrize(
    "field,value",
    [("before", "running"), ("cap", False), ("job_status", "failed"), ("torque", "enabled"), ("torque", "unknown")],
)
def test_unverified_recovery_never_enables_or_restarts(field, value):
    s = fixture()
    setattr(s, field, value)
    with pytest.raises(MotorRecoveryError) as error:
        s.run()
    assert "PRIVATE_NATIVE_LOG" not in str(error.value)
    assert all(p == "/api/daemon/reconnect-motors" for m, p in s.calls if m == "POST")


def test_job_deadline_is_bounded_and_does_not_reenqueue():
    s = fixture()
    s.job_status = "in_progress"
    with pytest.raises(MotorRecoveryError, match="finish in time"):
        s.run()
    assert s.jobs <= 4
    assert sum(m == "POST" for m, p in s.calls) == 1


@pytest.mark.parametrize("detail", ["Backend not running", "another 503", None])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_only_exact_pre_admission_refusal_can_authorize_recovery(detail, method):
    req = httpx.Request(method, "http://127.0.0.1:8000/api/motors/set_mode/enabled")
    res = httpx.Response(503, json={"detail": detail}, request=req)
    error = httpx.HTTPStatusError("x", request=req, response=res)
    assert backend_admission_rejected(error, method="POST", path="/api/motors/set_mode/enabled") is (
        detail == "Backend not running" and method == "POST"
    )


def test_transport_timeout_is_not_safe_to_recover_or_repeat():
    assert not backend_admission_rejected(
        httpx.ReadTimeout("ambiguous outcome"), method="POST", path="/api/motors/set_mode/enabled"
    )
