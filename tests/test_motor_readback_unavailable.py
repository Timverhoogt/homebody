"""Isolated safety-contract reproductions; all device I/O is mocked."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from homebody import power
from homebody.config import AppConfig
from homebody.runtime import HermesVoiceRuntime


def runtime_with_io(monkeypatch):
    calls = []
    media = SimpleNamespace(
        stop_playing=lambda: None,
        start_playing=lambda: None,
        stop_recording=lambda: None,
        start_recording=lambda: None,
        play_sound=lambda _: None,
    )
    robot = SimpleNamespace(media=media, stop_head_tracking=lambda: None, wake_up=lambda: calls.append("wake"))
    runtime = HermesVoiceRuntime(robot, threading.Event(), config_loader=lambda: AppConfig(api_key=""))

    def post(url, **kwargs):
        calls.append("POST:" + url.rsplit("/", 1)[-1])
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(power.httpx, "post", post)
    monkeypatch.setattr(power, "run_daemon_sleep", lambda: calls.append("native-sleep"))
    return runtime, calls


def failed_read(monkeypatch, calls, failure):
    def get(url, **kwargs):
        calls.append("GET:unavailable")
        request = httpx.Request("GET", url)
        if failure == "503":
            return httpx.Response(503, request=request)
        if failure == "connection":
            raise httpx.ConnectError("disconnected", request=request)
        raise httpx.ReadTimeout("unknown read outcome", request=request)

    monkeypatch.setattr(power.httpx, "get", get)


@pytest.mark.parametrize("failure", ["503", "connection", "timeout"])
def test_duplicate_awake_read_failure_must_not_authorize_fresh_wake(monkeypatch, failure):
    runtime, calls = runtime_with_io(monkeypatch)
    runtime._motors_enabled = True
    runtime._head_safely_folded = False
    failed_read(monkeypatch, calls, failure)
    error = None
    try:
        runtime.set_power_mode("awake")
    except RuntimeError as exc:
        error = str(exc)
    print({"calls": calls, "motors": runtime._motors_enabled, "error": error})
    assert calls == ["GET:unavailable"], "Unavailable read was treated as permission for new wake motion"
    assert error and runtime._motors_enabled is None


@pytest.mark.parametrize("failure", ["503", "connection", "timeout"])
def test_sleep_read_failure_must_not_reissue_enable_or_claim_known_torque(monkeypatch, failure):
    runtime, calls = runtime_with_io(monkeypatch)
    failed_read(monkeypatch, calls, failure)
    with pytest.raises(RuntimeError):
        runtime.set_power_mode("sleep")
    status = runtime.status()
    print(
        {
            "calls": calls,
            "motors": runtime._motors_enabled,
            "detail": status["detail"],
            "last_error": status["last_error"],
        }
    )
    assert calls == ["POST:enabled", "native-sleep", "GET:unavailable"], (
        "Backend read failure triggered another motor write"
    )
    assert runtime._motors_enabled is None
    assert "unverified" in str(status["detail"])


@pytest.mark.parametrize("error_class", [httpx.ReadTimeout, TimeoutError])
def test_wake_transport_failure_must_not_enter_recovery_fold(monkeypatch, error_class):
    runtime, calls = runtime_with_io(monkeypatch)

    def wake():
        calls.append("wake:timeout")
        raise error_class("backend lost during wake")

    runtime.robot.wake_up = wake
    failed_read(monkeypatch, calls, "connection")
    with pytest.raises(RuntimeError):
        runtime.set_power_mode("awake")
    print({"calls": calls, "motors": runtime._motors_enabled, "last_error": runtime.status()["last_error"]})
    assert "native-sleep" not in calls, "Wake transport failure triggered automatic recovery motion"
    assert calls.count("POST:enabled") == 1
    assert runtime._motors_enabled is None


def test_failed_reconfirmation_must_not_report_torque_enabled(monkeypatch):
    runtime, calls = runtime_with_io(monkeypatch)
    failed_read(monkeypatch, calls, "503")

    def post(url, **kwargs):
        calls.append("POST:enabled")
        code = 200 if calls.count("POST:enabled") == 1 else 503
        return httpx.Response(code, request=httpx.Request("POST", url))

    monkeypatch.setattr(power.httpx, "post", post)
    with pytest.raises(RuntimeError) as exc:
        runtime.set_power_mode("meeting")
    print(
        {
            "calls": calls,
            "motors": runtime._motors_enabled,
            "detail": runtime.status()["detail"],
            "last_error": str(exc.value),
        }
    )
    assert runtime._motors_enabled is None
    assert "motors remain enabled" not in str(exc.value), "Failure receipt contradicts unknown cached motor state"
    assert "unavailable" in str(exc.value)


@pytest.mark.parametrize("reason", ["movement_busy", "native_failure", "negative_pose"])
def test_failed_torque_preservation_propagates_unknown_result(monkeypatch, reason):
    runtime, calls = runtime_with_io(monkeypatch)
    if reason == "movement_busy":
        runtime._actions = SimpleNamespace(
            fence=lambda _reason: None, unfence=lambda: None, cancel=lambda **_: False, wait_idle=lambda **_: False
        )
    elif reason == "native_failure":

        def fail_sleep():
            raise RuntimeError("confirmed failed sleep")

        monkeypatch.setattr(power, "run_daemon_sleep", fail_sleep)
    else:
        monkeypatch.setattr(runtime, "_read_head_safely_folded", lambda: False)

    def post(url, **kwargs):
        calls.append("POST:enabled")
        count = calls.count("POST:enabled")
        code = 503 if reason == "movement_busy" or count == 2 else 200
        return httpx.Response(code, request=httpx.Request("POST", url))

    monkeypatch.setattr(power.httpx, "post", post)
    folded, error = runtime._fold_head_before_torque_release_unlocked()
    assert not folded and "unavailable" in error
    assert "motors remain enabled" not in error
    assert runtime._motors_enabled is None and runtime._head_safely_folded is False


@pytest.mark.parametrize("mode", ["awake", "sleep"])
@pytest.mark.parametrize("pose", [{"z": float("nan"), "pitch": 0}, {"z": -0.04, "pitch": float("inf")}, {}, None])
def test_invalid_pose_is_unknown_not_a_negative_measurement(monkeypatch, mode, pose):
    runtime, calls = runtime_with_io(monkeypatch)
    runtime._motors_enabled = True
    runtime._head_safely_folded = False
    monkeypatch.setattr(
        power.httpx,
        "get",
        lambda url, **_: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            content=json.dumps({"control_mode": "enabled", "head_pose": pose}),
        ),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        runtime.set_power_mode(mode)
    assert runtime._motors_enabled is None and runtime._head_safely_folded is False
    assert calls == ([] if mode == "awake" else ["POST:enabled", "native-sleep"])
