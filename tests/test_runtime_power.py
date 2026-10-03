from __future__ import annotations

import threading
import time

import pytest

from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.gesture_detection import GestureReactionGate
from reachy_mini_hermes.hermes_client import SpeechAudio
from reachy_mini_hermes.runtime import (
    HermesVoiceRuntime,
    PowerModeToolCall,
    RealtimePlayback,
    completed_camera_call_id,
    completed_power_mode_call,
    doa_yaw_degrees,
    realtime_audio_item_id,
    realtime_response_id,
)


class FakeMedia:
    def __init__(self) -> None:
        self.recording = False
        self.starts = 0
        self.stops = 0
        self.playing_starts = 0
        self.played: list[str] = []
        self.doa: tuple[float, bool] = (0.0, False)

    def start_playing(self) -> None:
        self.playing_starts += 1

    def play_sound(self, path: str) -> None:
        self.played.append(path)

    def get_DoA(self) -> tuple[float, bool]:
        return self.doa

    def start_recording(self) -> None:
        self.recording = True
        self.starts += 1

    def stop_recording(self) -> None:
        self.recording = False
        self.stops += 1

    def get_frame_jpeg(self) -> bytes:
        return b"test-jpeg"


class FakeRobot:
    def __init__(self) -> None:
        self.media = FakeMedia()
        self.tracking_started: list[float] = []
        self.tracking_stops = 0
        self.sleep_calls = 0

    def goto_sleep(self) -> None:
        self.sleep_calls += 1

    def start_head_tracking(self, weight: float = 1.0) -> None:
        self.tracking_started.append(weight)

    def stop_head_tracking(self) -> None:
        self.tracking_stops += 1


@pytest.fixture(autouse=True)
def fake_robot_fold_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests independent from a live daemon's `/api/state/full`."""
    original = HermesVoiceRuntime._read_head_safely_folded

    def read_folded(runtime: HermesVoiceRuntime) -> bool:
        robot = runtime.robot
        if isinstance(robot, FakeRobot):
            return runtime._head_safely_folded or robot.sleep_calls > 0
        return original(runtime)

    monkeypatch.setattr(HermesVoiceRuntime, "_read_head_safely_folded", read_folded)


def test_sleep_stops_microphone_and_standby_restores_local_wake() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]
    runtime._recording = True
    robot.media.recording = True

    sleep = runtime.set_power_mode("sleep")
    assert sleep["power_mode"] == "sleep"
    assert robot.media.recording is False
    assert robot.sleep_calls == 1
    assert motor_modes[-2:] == [True, False]

    standby = runtime.set_power_mode("standby")
    assert standby["power_mode"] == "standby"
    assert robot.media.recording is True
    assert motor_modes[-1] is False


def test_standby_folds_head_before_releasing_motor_torque() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]

    status = runtime.set_power_mode("standby")

    assert status["power_mode"] == "standby"
    assert robot.sleep_calls == 1
    assert motor_modes == [True, False]
    assert runtime._head_safely_folded is True


def test_already_folded_standby_does_not_replay_sleep_motion() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._head_safely_folded = True
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]

    runtime.set_power_mode("standby")

    assert robot.sleep_calls == 0
    assert motor_modes == [False]


def test_unverified_sleep_pose_keeps_torque_enabled() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._read_head_safely_folded = lambda: False  # type: ignore[method-assign]
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="without a verified folded pose"):
        runtime.set_power_mode("sleep")

    assert motor_modes == [True, True]
    assert runtime._head_safely_folded is False


def test_fold_refuses_torque_release_until_action_worker_is_idle() -> None:
    class BusyActions:
        pending_count = 1

        def cancel(self, *, stop_media: bool = True) -> bool:
            return True

        def wait_idle(self, timeout: float = 5.0) -> bool:
            return False

    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._actions = BusyActions()  # type: ignore[assignment]
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="movement did not stop"):
        runtime.set_power_mode("sleep")

    assert robot.sleep_calls == 0
    assert motor_modes == [True]
    assert runtime._head_safely_folded is False


def test_sleep_motion_failure_keeps_torque_enabled_instead_of_dropping_head() -> None:
    class FailingSleepRobot(FakeRobot):
        def goto_sleep(self) -> None:
            raise RuntimeError("movement blocked")

    runtime = HermesVoiceRuntime(FailingSleepRobot(), threading.Event())
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="motors remain enabled"):
        runtime.set_power_mode("sleep")
    status = runtime.status()

    assert motor_modes == [True, True]
    assert "motors remain enabled" in str(status["last_error"])
    assert status["power_mode"] == "sleep"


def test_robot_pose_fails_closed_on_missing_or_non_finite_daemon_fields(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {
                "head_pose": {"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 0},
                "body_yaw": float("nan"),
            }

    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.get", lambda *args, **kwargs: Response())
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())

    with pytest.raises(RuntimeError, match="head pose is incomplete"):
        runtime.robot_pose()


def test_robot_pose_converts_complete_daemon_state_to_ui_units(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {
                "head_pose": {
                    "x": 0.001,
                    "y": -0.002,
                    "z": 0.003,
                    "roll": 0.1,
                    "pitch": -0.2,
                    "yaw": 0.3,
                },
                "body_yaw": -0.4,
            }

    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.get", lambda *args, **kwargs: Response())
    pose = HermesVoiceRuntime(FakeRobot(), threading.Event()).robot_pose()

    assert pose == {
        "x": 1.0,
        "y": -2.0,
        "z": 3.0,
        "roll": 5.73,
        "pitch": -11.46,
        "yaw": 17.19,
        "body_yaw": -22.92,
    }


def test_meeting_mode_has_bounded_timer() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    status = runtime.set_power_mode("meeting", duration_seconds=1)
    assert status["power_mode"] == "meeting"
    remaining = status["meeting_seconds_remaining"]
    assert isinstance(remaining, int)
    assert 59 <= remaining <= 60


@pytest.mark.parametrize("mode", ["standby", "meeting", "sleep"])
def test_privacy_power_modes_invalidate_agent_session(mode: str) -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    agent = runtime.status()["agent"]
    assert isinstance(agent, dict)
    generation = int(agent["session_generation"])

    runtime.set_power_mode(mode, duration_seconds=60)

    assert runtime.agent_session_is_current(generation) is False
    current = runtime.status()["agent"]
    assert isinstance(current, dict)
    assert current["current_task"] == ""


def test_awake_runs_physical_wake_motion() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    calls: list[tuple[bool, bool]] = []
    runtime._set_motor_mode = (  # type: ignore[method-assign]
        lambda enabled, wake=False: calls.append((enabled, wake))
    )

    status = runtime.set_power_mode("awake")

    assert status["power_mode"] == "awake"
    assert calls[-1] == (True, True)


def test_starting_runtime_rejects_awake_before_any_motion() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    calls: list[tuple[bool, bool]] = []
    runtime._runtime_started = True
    runtime._set_motor_mode = (  # type: ignore[method-assign]
        lambda enabled, wake=False: calls.append((enabled, wake))
    )

    with pytest.raises(RuntimeError, match="still starting; no power transition was attempted"):
        runtime.set_power_mode("awake")

    assert calls == []
    assert runtime.status()["power_mode"] == "standby"


def test_wake_timeout_after_confirmed_unfolded_pose_reports_success(monkeypatch) -> None:
    class LateWakeRobot(FakeRobot):
        def wake_up(self) -> None:
            raise TimeoutError("native wake task timed out")

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"head_pose": {"z": 0.0, "pitch": 0.0}}

    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.post", lambda *args, **kwargs: Response())
    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.get", lambda *args, **kwargs: Response())
    runtime = HermesVoiceRuntime(LateWakeRobot(), threading.Event())

    status = runtime.set_power_mode("awake")

    assert status["power_mode"] == "awake"
    assert status["motors_enabled"] is True
    assert status["last_error"] == ""


def test_realtime_response_id_identifies_late_interrupted_output() -> None:
    assert realtime_response_id("response.created", {"response": {"id": "resp-1"}}) == "resp-1"
    assert realtime_response_id("response.audio.delta", {"response_id": "resp-1"}) == "resp-1"


def test_motor_daemon_failure_is_reported_instead_of_false_standby(monkeypatch) -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._head_safely_folded = True

    def fail_post(*args, **kwargs):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.post", fail_post)

    with pytest.raises(RuntimeError, match="disabling motor torque failed"):
        runtime.set_power_mode("standby")

    status = runtime.status()
    assert status["motors_enabled"] is None
    assert status["head_safely_folded"] is True
    assert "daemon unavailable" in str(status["last_error"])


def test_wake_failure_recovers_to_confirmed_folded_standby(monkeypatch) -> None:
    class FailingWakeRobot(FakeRobot):
        def wake_up(self) -> None:
            raise RuntimeError("wake blocked")

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.post", lambda *args, **kwargs: Response())
    runtime = HermesVoiceRuntime(FailingWakeRobot(), threading.Event())
    runtime._head_safely_folded = True

    with pytest.raises(RuntimeError, match="wake motion failed"):
        runtime.set_power_mode("awake")

    status = runtime.status()
    assert status["power_mode"] == "standby"
    assert status["motors_enabled"] is False
    assert status["head_safely_folded"] is True
    assert "wake blocked" in str(status["last_error"])


def test_disable_failure_after_folding_keeps_enabled_torque_visible(monkeypatch) -> None:
    class Response:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def raise_for_status(self) -> None:
            if self.fail:
                raise RuntimeError("disable rejected")

    monkeypatch.setattr(
        "reachy_mini_hermes.runtime.httpx.post",
        lambda url, **kwargs: Response(url.endswith("/disabled")),
    )
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())

    with pytest.raises(RuntimeError, match="disabling motor torque failed"):
        runtime.set_power_mode("standby")

    status = runtime.status()
    assert status["power_mode"] == "standby"
    assert status["motors_enabled"] is True
    assert status["head_safely_folded"] is True
    assert "disable rejected" in str(status["last_error"])


def test_power_transitions_are_serialized_across_clients() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    state_lock = threading.Lock()
    barrier = threading.Barrier(3)
    active = 0
    max_active = 0
    errors: list[Exception] = []

    def apply() -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        mode = runtime._effective_power_mode()
        runtime._set_status("test", mode, power_mode=mode, last_error="")
        with state_lock:
            active -= 1

    runtime._apply_power_mode_unlocked = apply  # type: ignore[method-assign]

    def transition(mode: str) -> None:
        barrier.wait()
        try:
            runtime.set_power_mode(mode)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=transition, args=("awake",)),
        threading.Thread(target=transition, args=("standby",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    assert not errors
    assert max_active == 1
    assert all(not thread.is_alive() for thread in threads)


def test_robot_action_execution_rechecks_confirmed_torque() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._power_mode = "standby"
    runtime._motors_enabled = False

    with pytest.raises(RuntimeError, match="torque is not confirmed"):
        runtime._before_robot_action()


def test_realtime_playback_remains_audible_after_generation_finishes() -> None:
    playback = RealtimePlayback(item_id="item-123")
    playback.add(now=10.0, duration_seconds=2.0)
    playback.add(now=10.1, duration_seconds=2.0)

    assert playback.audible(13.0)
    assert playback.played_ms(11.5) == 1500
    assert not playback.audible(14.1)

    playback.reset()
    assert playback.item_id == ""
    assert playback.played_ms(15.0) == 0


def test_realtime_audio_item_tracking_ignores_function_calls() -> None:
    function_call = {
        "item": {
            "id": "item-function",
            "type": "function_call",
            "name": "express_reachy_emotion",
        }
    }
    assistant_message = {
        "item": {
            "id": "item-message",
            "type": "message",
            "role": "assistant",
        }
    }

    assert realtime_audio_item_id("response.output_item.added", function_call) == ""
    assert realtime_audio_item_id("response.output_item.added", assistant_message) == "item-message"
    assert (
        realtime_audio_item_id(
            "response.output_audio.delta",
            {"item_id": "item-audio", "delta": "ignored"},
        )
        == "item-audio"
    )


def test_power_mode_tool_requires_completed_output_item() -> None:
    completed: dict[str, object] = {
        "item": {
            "type": "function_call",
            "name": "set_reachy_power_mode",
            "status": "completed",
            "call_id": "call-power",
            "arguments": '{"mode":"meeting","duration_minutes":45}',
        }
    }
    incomplete: dict[str, object] = {
        "item": {**completed["item"], "status": "incomplete"},  # type: ignore[dict-item]
    }

    assert completed_power_mode_call("response.function_call_arguments.done", completed) is None
    assert completed_power_mode_call("response.output_item.done", incomplete) is None
    call = completed_power_mode_call("response.output_item.done", completed)
    assert call is not None
    assert call.call_id == "call-power"
    assert call.mode == "meeting"
    assert call.duration_minutes == 45


def test_standby_requests_current_conversation_to_stop_and_awake_clears_it() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]

    runtime.set_power_mode("standby")
    assert runtime._conversation_stop_requested.is_set()

    runtime.set_power_mode("awake")
    assert not runtime._conversation_stop_requested.is_set()


def test_power_mode_tool_applies_meeting_before_ending_realtime_session() -> None:
    class Session:
        def __init__(self) -> None:
            self.results: list[tuple[str, dict[str, object], bool]] = []

        def send_tool_result(
            self,
            call_id: str,
            result: dict[str, object],
            *,
            continue_response: bool = True,
        ) -> None:
            self.results.append((call_id, result, continue_response))

    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    session = Session()

    result = runtime._handle_power_mode_call(
        session,  # type: ignore[arg-type]
        PowerModeToolCall("call-meeting", "meeting", 45),
    )

    assert result == {"ok": True, "mode": "meeting", "duration_minutes": 45}
    assert runtime.status()["power_mode"] == "meeting"
    assert runtime._conversation_stop_requested.is_set()
    assert session.results == [("call-meeting", result, False)]


def test_manual_robot_action_auto_wakes_and_manual_stop_preserves_audio_pipeline() -> None:
    class Actions:
        def __init__(self) -> None:
            self.queued: list[tuple[str, dict[str, object], bool]] = []

        @property
        def pending_count(self) -> int:
            return 1

        def enqueue(
            self,
            name: str,
            arguments: dict[str, object],
            *,
            hold_pose: bool = False,
            reject_if_busy: bool = False,
        ) -> dict[str, object]:
            self.queued.append((name, arguments, hold_pose))
            assert reject_if_busy is True
            return {"accepted": True, "queued": name}

        def cancel(self, *, stop_media: bool = True) -> bool:
            assert stop_media is False
            return True

        def wait_idle(self, timeout: float = 5.0) -> bool:
            return True

    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())

    def set_motor_mode(enabled: bool, wake: bool = False) -> None:
        runtime._motors_enabled = enabled
        if enabled and wake:
            runtime._head_safely_folded = False

    runtime._set_motor_mode = set_motor_mode  # type: ignore[method-assign]
    actions = Actions()
    runtime._actions = actions  # type: ignore[assignment]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    agent = runtime.status()["agent"]
    assert isinstance(agent, dict)
    agent_generation = int(agent["session_generation"])

    result = runtime.queue_manual_robot_action("look", "left")

    assert result["ok"] is True
    assert result["power_mode"] == "awake"
    assert actions.queued == [("move_reachy_head", {"direction": "left"}, True)]
    assert runtime.status()["power_mode"] == "awake"

    stopped = runtime.stop_manual_robot_action()
    assert stopped == {
        "ok": True,
        "robot_stopped": True,
        "active_cancelled": True,
        "queued_cancelled": 0,
    }
    assert runtime.agent_session_is_current(agent_generation) is False
    assert robot.media.playing_starts == 0


def test_manual_stop_raises_when_action_worker_does_not_become_idle() -> None:
    class Actions:
        pending_count = 1

        @staticmethod
        def cancel(*, stop_media: bool = True) -> bool:
            assert stop_media is False
            return True

        @staticmethod
        def wait_idle(timeout: float = 5.0) -> bool:
            assert timeout == 5.0
            return False

    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._actions = Actions()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="did not become idle after Stop"):
        runtime.stop_manual_robot_action()


def test_manual_robot_action_is_blocked_in_privacy_modes() -> None:
    class Actions:
        pending_count = 0

        def cancel(self, *, stop_media: bool = True) -> bool:
            return False

        def wait_idle(self, timeout: float = 5.0) -> bool:
            return True

    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    runtime._actions = Actions()  # type: ignore[assignment]
    runtime.set_power_mode("sleep")

    with pytest.raises(RuntimeError, match="blocked"):
        runtime.queue_manual_robot_action("dance", "short")


def test_manual_stop_does_not_restore_playback_or_motion_in_privacy_mode() -> None:
    class Actions:
        pending_count = 1

        def cancel(self, *, stop_media: bool = True) -> bool:
            assert stop_media is False
            return True

        def wait_idle(self, timeout: float = 5.0) -> bool:
            return True

    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._actions = Actions()  # type: ignore[assignment]
    runtime._power_mode = "sleep"
    runtime._privacy_requested.set()

    stopped = runtime.stop_manual_robot_action()

    assert stopped["active_cancelled"] is True
    assert robot.media.playing_starts == 0
    assert runtime._playback_stopped_for_privacy is False
    with pytest.raises(RuntimeError, match="privacy"):
        runtime._before_robot_action()


def test_camera_test_captures_locally_without_returning_image() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())

    result = runtime.test_camera()

    assert result == {"bytes": 9, "content_type": "image/jpeg"}
    assert runtime.status()["camera_captures"] == 1


def test_runtime_status_reports_confirmed_motor_and_fold_state(monkeypatch) -> None:
    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr("reachy_mini_hermes.runtime.httpx.post", lambda *args, **kwargs: Response())
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._head_safely_folded = False

    runtime._set_motor_mode(True)
    status = runtime.status()

    assert status["motors_enabled"] is True
    assert status["head_safely_folded"] is False


@pytest.mark.parametrize("mode", ["meeting", "sleep"])
def test_local_camera_capture_is_blocked_in_privacy_modes(mode: str) -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._power_mode = mode
    runtime._meeting_until = float("inf") if mode == "meeting" else 0.0
    runtime._privacy_requested.set()

    with pytest.raises(RuntimeError, match="privacy"):
        runtime.camera_snapshot()

    assert runtime.status()["camera_captures"] == 0


def test_camera_capture_rechecks_privacy_after_frame_arrives() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())

    def enter_sleep_during_capture() -> bytes:
        runtime._power_mode = "sleep"
        runtime._privacy_requested.set()
        return b"late-frame"

    robot.media.get_frame_jpeg = enter_sleep_during_capture  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="privacy"):
        runtime.camera_snapshot()

    assert runtime.status()["camera_captures"] == 0


def test_camera_capture_requires_completed_output_item() -> None:
    completed: dict[str, object] = {
        "item": {
            "type": "function_call",
            "name": "capture_reachy_camera",
            "status": "completed",
            "call_id": "call-camera",
        }
    }
    cancelled: dict[str, object] = {
        "item": {
            "type": "function_call",
            "name": "capture_reachy_camera",
            "status": "incomplete",
            "call_id": "call-camera",
        }
    }

    assert completed_camera_call_id("response.output_item.done", completed) == "call-camera"
    assert completed_camera_call_id("response.function_call_arguments.done", completed) == ""
    assert completed_camera_call_id("response.output_item.done", cancelled) == ""


def test_privacy_mode_stops_local_face_tracking() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    runtime._set_face_tracking(True, weight=0.65)

    assert runtime.status()["face_tracking_active"] is True
    runtime.set_power_mode("meeting", duration_seconds=60)

    assert robot.tracking_stops == 1
    assert runtime.status()["face_tracking_active"] is False


def test_doa_angle_is_converted_to_reachy_yaw_with_deadband_and_clamp() -> None:
    assert doa_yaw_degrees(1.5707963267948966) == 0.0
    assert doa_yaw_degrees(0.7853981633974483) == 36.0
    assert doa_yaw_degrees(0.0) == 60.0


def test_privacy_action_cancel_restarts_playback_on_standby() -> None:
    class Actions:
        pending_count = 0

        def cancel(self, *, stop_media: bool = True) -> bool:
            return True

        def wait_idle(self, timeout: float = 5.0) -> bool:
            return True

    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._actions = Actions()  # type: ignore[assignment]
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]

    runtime.set_power_mode("sleep")
    runtime.set_power_mode("standby")

    assert robot.media.playing_starts == 1


def test_doa_requires_current_speech_detection() -> None:
    class Motion:
        def __init__(self) -> None:
            self.yaws: list[float] = []

        def orient_to_sound(self, yaw: float) -> None:
            self.yaws.append(yaw)

    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    motion = Motion()
    runtime._motion = motion  # type: ignore[assignment]
    config = AppConfig(doa_enabled=True)

    robot.media.doa = (0.7853981633974483, False)
    runtime._orient_to_voice(config)
    assert motion.yaws == []

    robot.media.doa = (0.7853981633974483, True)
    runtime._orient_to_voice(config)
    assert motion.yaws == [36.0]


def test_pipeline_playback_does_not_start_in_privacy_mode() -> None:
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    runtime.set_power_mode("sleep")
    played_before = list(robot.media.played)

    result = runtime._play_response(SpeechAudio(b"audio", "audio/mpeg", ".mp3", "test"), "hello")

    assert result is False
    assert robot.media.played == played_before


def test_gesture_iteration_is_awake_private_confirmed_and_edge_triggered() -> None:
    class Detector:
        def detect_jpeg(self, jpeg: bytes) -> tuple[str, float]:
            assert jpeg == b"gesture-jpeg"
            return "palm", 0.90

    class Actions:
        busy = False
        pending_count = 0

    captures: list[bool] = []
    queued: list[tuple[str, str, bool]] = []
    runtime = HermesVoiceRuntime(
        FakeRobot(),
        threading.Event(),
        config_loader=lambda: AppConfig(
            home_assistant_enabled=True,
            home_assistant_controls_enabled=True,
            gesture_detection_enabled=True,
        ),
    )
    runtime._actions = Actions()  # type: ignore[assignment]
    runtime._capture_camera_jpeg = lambda: captures.append(True) or b"gesture-jpeg"  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="never wake"):
        runtime.queue_manual_robot_action("emotion", "welcoming", wake_if_standby=False)
    assert runtime._power_mode == "standby"
    runtime.queue_manual_robot_action = (  # type: ignore[method-assign]
        lambda action, value, *, wake_if_standby=True: queued.append((action, value, wake_if_standby)) or {"ok": True}
    )
    gate = GestureReactionGate(required_frames=3, clear_frames=2, cooldown_seconds=8.0, min_confidence=0.70)

    assert runtime._process_gesture_once(Detector(), gate, now=1.0) is False
    assert captures == []
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    assert runtime._process_gesture_once(Detector(), gate, now=2.0) is True
    assert runtime._process_gesture_once(Detector(), gate, now=2.4) is True
    assert runtime._process_gesture_once(Detector(), gate, now=2.8) is True
    assert runtime._process_gesture_once(Detector(), gate, now=3.2) is True
    assert queued == [("emotion", "welcoming", False)]
    status = runtime.status()
    assert status["gesture_detected"] == "palm"
    assert status["gesture_confidence"] == 0.90
    assert status["gesture_reactions"] == 1

    runtime._kids_active = True
    assert runtime._process_gesture_once(Detector(), gate, now=4.0) is False
    assert len(captures) == 4


def test_awake_transition_cannot_resume_a_stopped_voice_turn() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    runtime._accept_wake_turn()
    assert runtime._turn_stop_requested() is False

    # Kids start, a profile change, or a phone Stop ends the in-flight turn ...
    runtime._request_conversation_stop()
    # ... and a later Awake transition (I Spy, manual control) clears the shared event.
    runtime.set_power_mode("awake")

    assert not runtime._conversation_stop_requested.is_set()
    assert runtime._turn_stop_requested() is True

    runtime._accept_wake_turn()
    assert runtime._turn_stop_requested() is False
