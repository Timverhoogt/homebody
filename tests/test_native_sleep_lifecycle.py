"""Power lifecycle regressions around the real native-sleep boundary."""

import threading
from types import SimpleNamespace

import pytest

from homebody import power
from homebody.motion import VoiceMotion
from homebody.runtime import HermesVoiceRuntime


def make_runtime(monkeypatch):
    events = []

    def sdk_sleep():
        pytest.fail("Homebody must never use the client SDK sleep choreography")

    robot = SimpleNamespace(
        goto_sleep=sdk_sleep,
        goto_target=lambda **kwargs: events.append("voice-pose"),
        enable_wobbling=lambda: events.append("wobble-on"),
        disable_wobbling=lambda: events.append("wobble-off"),
        wake_up=lambda: events.append("wake"),
        stop_head_tracking=lambda: None,
        media=SimpleNamespace(
            stop_playing=lambda: events.append("playback-stop"),
            start_playing=lambda: events.append("playback-start"),
            stop_recording=lambda: events.append("recording-stop"),
            start_recording=lambda: events.append("recording-start"),
            play_sound=lambda path: None,
        ),
    )
    runtime = HermesVoiceRuntime(robot, threading.Event())
    robot.media.enable_wobbling = lambda callback: events.append("wobble-on")
    robot.client = SimpleNamespace(send_command=lambda command: None)
    runtime._motion = VoiceMotion(robot, playback=runtime._playback)
    runtime._read_head_safely_folded = lambda: True
    monkeypatch.setattr(power, "run_daemon_sleep", lambda: events.append("daemon-sleep"))

    class Response:
        def raise_for_status(self):
            pass

    def motor_post(url, **kwargs):
        events.append("torque-on" if url.endswith("/enabled") else "torque-off")
        return Response()

    monkeypatch.setattr(power.httpx, "post", motor_post)
    return runtime, events


@pytest.mark.parametrize("mode", ["standby", "sleep", "meeting"])
def test_every_fold_uses_native_daemon_and_not_sdk_choreography(monkeypatch, mode):
    rt, events = make_runtime(monkeypatch)
    rt._motion.speaking()
    events.clear()
    rt.set_power_mode(mode)
    assert events.index("playback-stop") < events.index("wobble-off")
    assert events.index("wobble-off") < events.index("daemon-sleep") < events.index("torque-off")
    count = len(events)
    rt._motion.resume()
    rt._motion.speaking()
    rt._motion.idle()
    assert len(events) == count
    assert rt._motors_enabled is False
    assert rt._head_safely_folded is True
    if mode == "standby":
        assert events[-1] == "recording-start"
        assert rt._recording is True
    else:
        assert rt._recording is False


def test_automatic_return_to_standby_restores_capture_but_not_wobbling(monkeypatch):
    rt, events = make_runtime(monkeypatch)
    rt._recording = True
    rt._motion.speaking()
    assert rt._fold_head_before_torque_release() == (True, "")
    assert events[-1] == "recording-start"
    assert rt._recording is True
    assert rt._motion._power_suspended is True


def test_direct_wake_word_motor_path_releases_motion_fence_only_after_wake(monkeypatch):
    rt, events = make_runtime(monkeypatch)
    rt.set_power_mode("standby")
    rt._set_motor_mode(True, wake=True)
    assert events.index("wake") < events.index("playback-start")
    rt._motion.speaking()
    assert events[-1] == "wobble-on"


def test_unknown_native_outcome_blocks_later_wake_and_duplicate_sleep(monkeypatch):
    rt, events = make_runtime(monkeypatch)

    def ambiguous():
        events.append("daemon-sleep")
        raise power.DaemonSleepError("lost response", uncertain=True)

    monkeypatch.setattr(power, "run_daemon_sleep", ambiguous)
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        rt.set_power_mode("sleep")
    assert rt._motors_enabled is None
    assert "torque-off" not in events
    count = len(events)
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        rt._set_motor_mode(True, wake=True)
    assert rt._fold_head_before_torque_release()[0] is False
    assert len(events) == count


def test_failed_local_quiescence_never_admits_a_daemon_movement(monkeypatch):
    rt, events = make_runtime(monkeypatch)
    rt._motion.speaking()

    def fail():
        raise RuntimeError("playback still live")

    rt.robot.media.stop_playing = fail
    with pytest.raises(RuntimeError, match="playback still live"):
        rt.set_power_mode("sleep")
    assert "daemon-sleep" not in events
    assert "wobble-off" not in events
    assert "torque-off" not in events
