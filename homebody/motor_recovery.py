"""Wake-scoped motor backend recovery through the optional local SDK capability.

Never starts a process, enables torque, wakes/folds, or retries ambiguous writes.
The compatibility endpoint must rebuild only the lost torque-off motor backend.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable

import httpx

_BASE = "http://127.0.0.1:8000"
_COMMAND = "daemon-reconnect-motors"


class MotorRecoveryError(RuntimeError):
    """Fixed recovery categories only; native job logs never enter public status."""


def replace_control_client(robot, *, current: Callable[[], bool], connect=None) -> None:
    """Replace only the lost control socket, never construct robot/media.

    Once admitted, finish read-only connection cleanup even if Stop arrives;
    the caller rechecks original wake authority before any motor command.
    """
    if not current():
        raise MotorRecoveryError("Motor reconnection cancelled before control connection admission")

    def connect_local():
        from reachy_mini.io.ws_client import WSClient

        return WSClient("127.0.0.1", 8000)

    factory = connect if connect is not None else connect_local
    fresh = None
    try:
        robot.client.disconnect()
        fresh = factory()
        fresh.wait_for_connection(timeout=3.0)
        robot.client = fresh
    except Exception as exc:
        if fresh is not None:
            try:
                fresh.disconnect()
            except Exception as cleanup:
                raise MotorRecoveryError("Motor control socket cleanup failed; no wake was attempted") from cleanup
        raise MotorRecoveryError("Motor control socket did not reconnect; no wake was attempted") from exc


def backend_admission_rejected(exc: BaseException | None, *, method: str, path: str) -> bool:
    """Only the SDK's exact pre-admission rejection is eligible, never a timeout."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 503:
        return False
    try:
        request = exc.request
        return (
            request.method == method
            and request.url.path == path
            and request.url.scheme == "http"
            and request.url.host == "127.0.0.1"
            and request.url.port == 8000
            and exc.response.json() == {"detail": "Backend not running"}
        )
    except (ValueError, RuntimeError, AttributeError):
        return False


def recover_motor_backend(
    *,
    current: Callable[[], bool],
    client=httpx,
    wait: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 15.0,
) -> dict:
    """One bounded attempt; every subsequent wake owns a new independent admission."""
    deadline = clock() + timeout

    def check():
        if not current():
            raise MotorRecoveryError("Motor reconnection cancelled by Stop, privacy or a newer transition")
        if clock() >= deadline:
            raise MotorRecoveryError("Motor reconnection did not finish in time; no wake was attempted")

    def call(path, *, post=False):
        check()
        remaining = max(0.05, min(2.0, deadline - clock()))
        try:
            response = (
                client.post(_BASE + path, json={}, timeout=remaining)
                if post
                else client.get(_BASE + path, timeout=remaining)
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid envelope")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MotorRecoveryError("Local motor reconnection is unavailable; check motor power and USB") from exc
        check()
        return data

    before = call("/api/daemon/status")
    if before.get("state") != "error" or "backend_status" not in before or before["backend_status"] is not None:
        raise MotorRecoveryError("Motor reconnection refused: the backend is not a confirmed absent error state")
    caps = call("/api/daemon/motor-recovery")
    if (
        caps.get("supported") is not True
        or type(caps.get("schema")) is not int
        or caps["schema"] != 1
        or caps.get("torque_off_only") is not True
    ):
        raise MotorRecoveryError("This Reachy daemon does not support safe wake-time motor reconnection")
    admission = call("/api/daemon/reconnect-motors", post=True)
    job_id = admission.get("job_id")
    try:
        if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
            raise ValueError("invalid job id")
    except (ValueError, TypeError, AttributeError) as exc:
        raise MotorRecoveryError("Motor reconnection returned an invalid job receipt; no wake was attempted") from exc
    while True:
        job = call("/api/apps/job-status/" + job_id)
        if job.get("command") != _COMMAND:
            raise MotorRecoveryError("Motor reconnection job did not match the admitted operation")
        status = job.get("status")
        if status == "done":
            break
        if status not in {"pending", "in_progress"}:
            raise MotorRecoveryError("Motor reconnection failed; no wake was attempted")
        wait(0.1)
    after = call("/api/daemon/status")
    if after.get("state") != "running" or not isinstance(after.get("backend_status"), dict) or after.get("error"):
        raise MotorRecoveryError("Motor reconnection did not restore a healthy backend; no wake was attempted")
    state = call("/api/state/full")  # SDK dependency requires its actual ready event.
    pose = state.get("head_pose")
    if state.get("control_mode") != "disabled" or not isinstance(pose, dict):
        raise MotorRecoveryError("Motor reconnection did not verify disabled torque; no wake was attempted")
    if any(
        type(pose.get(k)) not in {int, float} or not math.isfinite(pose[k])
        for k in ("x", "y", "z", "roll", "pitch", "yaw")
    ):
        raise MotorRecoveryError("Motor reconnection pose is unverified; no wake was attempted")
    check()
    return state
