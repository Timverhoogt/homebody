"""M2: robot-action admission must be fenced for the whole safety fold, not drained once.

The real ``ReachyRobotActions`` worker, fold and power transition run here; the daemon HTTP
edge, the SDK pose helper and the native sleep are fixtures. Races are driven by events.
"""

from __future__ import annotations

import threading

import httpx
import pytest

from homebody.config import AppConfig
from homebody.robot_tools import ReachyRobotActions
from homebody.runtime import HermesVoiceRuntime

_CANCELLED = {"ok": False, "error": "Robot action was cancelled", "action": "move_reachy_head"}


class _Media:
    def __init__(self) -> None:
        self.recording = False

    def start_playing(self) -> None:
        pass

    def stop_playing(self) -> None:
        pass

    def play_sound(self, path: str) -> None:
        pass

    def start_recording(self) -> None:
        self.recording = True

    def stop_recording(self) -> None:
        self.recording = False


class _Robot:
    def __init__(self) -> None:
        self.media = _Media()
        self.moves: list[dict[str, object]] = []

    def get_current_head_pose(self) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

    def goto_target(self, **kwargs: object) -> None:
        self.moves.append(kwargs)

    def set_target_head_pose(self, head: object) -> None:
        pass

    def set_target_body_yaw(self, body_yaw: float) -> None:
        pass

    def set_target_antenna_joint_positions(self, positions: list[float]) -> None:
        pass

    def cancel_move(self) -> None:
        pass

    def wake_up(self) -> None:
        pass

    def stop_head_tracking(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _hardware_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(
        "homebody.power.httpx.post",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("POST", url)),
    )


def _runtime_with_worker() -> tuple[HermesVoiceRuntime, _Robot, ReachyRobotActions]:
    robot = _Robot()
    stop_event = threading.Event()
    runtime = HermesVoiceRuntime(
        robot,
        stop_event,
        config_loader=lambda: AppConfig(api_key="test-key", robot_tools_enabled=True),
    )
    runtime._control_ready.set()
    runtime._audio_ready = True
    runtime._read_head_safely_folded = lambda: True  # type: ignore[method-assign]
    actions = ReachyRobotActions(
        robot,
        stop_event,
        library_factory=lambda: None,
        before_action=runtime._before_robot_action,
        after_action=runtime._after_robot_action,
        on_result=runtime._on_robot_action_result,
    )
    runtime._actions = actions
    actions.start()
    return runtime, robot, actions


class _BlockedSleep:
    """Hold the daemon sleep open until the test releases it; record entry/exit order."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.events: list[str] = []

    def __call__(self) -> None:
        self.events.append("enter")
        self.entered.set()
        assert self.release.wait(5)
        self.events.append("exit")


def test_late_admission_during_native_sleep_is_refused_and_fold_releases_torque_safely() -> None:
    runtime, robot, actions = _runtime_with_worker()
    sleep = _BlockedSleep()
    runtime._run_native_sleep = sleep  # type: ignore[method-assign]
    outcome: dict[str, object] = {}
    fold = threading.Thread(target=lambda: outcome.update(runtime.set_power_mode("standby")))
    try:
        fold.start()
        assert sleep.entered.wait(5)
        assert runtime._motors_enabled is True, "torque is enabled for the fold while the sleep runs"

        direct = actions.enqueue("move_reachy_head", {"direction": "left"})
        everyday = runtime._everyday_robot_action(
            "move_reachy_head", {"direction": "left"}, runtime.everyday_tool_fence()
        )
    finally:
        sleep.release.set()
        fold.join(5)
    assert not fold.is_alive()

    assert direct.get("accepted") is None and direct["code"] == "safety_fold"
    assert "safety fold" in str(direct["error"])
    assert everyday.get("accepted") is None and "safety fold" in str(everyday["error"])
    assert robot.moves == []
    assert outcome["last_error"] == ""
    assert outcome["motors_enabled"] is False
    assert outcome["head_safely_folded"] is True
    assert actions.pending_count == 0 and not actions.busy
    assert actions.fence_reason == ""

    # After the fold the controller admits again and the existing policy decides.
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    completed = threading.Event()
    results: list[dict[str, object]] = []

    def done(result: dict[str, object]) -> None:
        results.append(result)
        completed.set()

    assert actions.enqueue("move_reachy_head", {"direction": "left"}, on_complete=done)["accepted"] is True
    assert completed.wait(5)
    assert results[0]["ok"] is True
    assert len(robot.moves) == 1
    actions.close()


def test_fence_alone_refuses_worker_admission_of_an_already_queued_action() -> None:
    robot = _Robot()
    stop_event = threading.Event()
    before: list[str] = []
    actions = ReachyRobotActions(
        robot, stop_event, library_factory=lambda: None, before_action=lambda: before.append("x")
    )
    results: list[dict[str, object]] = []
    assert actions.enqueue("move_reachy_head", {"direction": "left"}, on_complete=results.append)["accepted"] is True
    assert actions.pending_count == 1

    # No generation bump here: the fence by itself must stop the worker from admitting.
    actions.fence("Robot action was blocked by a safety fold")
    actions.start()
    assert actions.wait_idle(timeout=5)

    assert results == [_CANCELLED]
    assert before == [] and robot.moves == []
    refused = actions.enqueue("move_reachy_head", {"direction": "left"})
    assert refused == {"ok": False, "error": "Robot action was blocked by a safety fold", "code": "safety_fold"}

    actions.unfence()
    completed = threading.Event()
    admitted = actions.enqueue("move_reachy_head", {"direction": "left"}, on_complete=lambda r: completed.set())
    assert admitted["accepted"] is True
    assert completed.wait(5)
    assert before == ["x"] and len(robot.moves) == 1
    actions.close()


def test_fold_fails_closed_when_an_unfenced_controller_is_busy_at_release_time() -> None:
    class InertActions:
        """A controller that ignores the fence, as the corroborating reviewer's fake did."""

        def __init__(self) -> None:
            self.busy = False
            self.pending_count = 0
            self.fences: list[str] = []

        def fence(self, reason: str) -> None:
            self.fences.append(reason)

        def unfence(self) -> None:
            self.fences.append("")

        def cancel(self, *, stop_media: bool = True) -> bool:
            return False

        def wait_idle(self, timeout: float = 5.0) -> bool:
            return self.pending_count == 0 and not self.busy

    robot = _Robot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    fake = InertActions()
    runtime._actions = fake  # type: ignore[assignment]
    runtime._read_head_safely_folded = lambda: True  # type: ignore[method-assign]
    motor_modes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_modes.append(enabled)  # type: ignore[method-assign]

    def sleep_with_a_move_beside_it() -> None:
        fake.busy = True

    runtime._run_native_sleep = sleep_with_a_move_beside_it  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="during the safety fold"):
        runtime.set_power_mode("sleep")

    assert motor_modes == [True, True], "torque is preserved, never released under a running move"
    assert runtime._head_safely_folded is False
    assert fake.fences == ["Robot action was blocked by a safety fold", ""]
    status = runtime.status()
    assert "motors remain enabled" in str(status["last_error"])


def test_stop_completes_during_a_blocked_fold_and_sleep_waits_its_turn() -> None:
    runtime, robot, actions = _runtime_with_worker()
    sleep = _BlockedSleep()
    runtime._run_native_sleep = sleep  # type: ignore[method-assign]
    results: dict[str, object] = {}
    fold = threading.Thread(target=lambda: results.update(standby=runtime.set_power_mode("standby")))
    stop = threading.Thread(target=lambda: results.update(stop=runtime.stop_manual_robot_action()))
    privacy = threading.Thread(target=lambda: results.update(sleep=runtime.set_power_mode("sleep")))
    try:
        fold.start()
        assert sleep.entered.wait(5)
        # Physical Stop never waits for the motor transition lock, so it completes mid-fold.
        stop.start()
        stop.join(5)
        assert not stop.is_alive()
        assert results["stop"]["ok"] is True  # type: ignore[index]
        privacy.start()
    finally:
        sleep.release.set()
        fold.join(5)
        privacy.join(5)
    assert not fold.is_alive() and not privacy.is_alive()

    # The Sleep transition's own fold ran strictly after the Standby fold finished.
    assert sleep.events == ["enter", "exit", "enter", "exit"]
    assert results["standby"]["last_error"] == ""  # type: ignore[index]
    assert results["sleep"]["power_mode"] == "sleep"  # type: ignore[index]
    assert runtime._privacy_requested.is_set()
    assert runtime._motors_enabled is False and runtime._head_safely_folded is True
    assert actions.fence_reason == ""
    assert robot.moves == []
    actions.close()
