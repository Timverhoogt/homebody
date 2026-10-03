from __future__ import annotations

import math
import threading
import time
from typing import cast

import pytest

from homebody.robot_tools import (
    CameraJoystickStream,
    ReachyRobotActions,
    completed_robot_tool_call,
    manual_precision_action,
    manual_robot_action,
    robot_control_options,
)


class FakeMove:
    duration = 0.04

    def evaluate(self, elapsed: float) -> tuple[dict[str, float], list[float], float]:
        return {"elapsed": elapsed}, [elapsed, -elapsed], elapsed


class FakeLibrary:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, name: str) -> FakeMove:
        self.requested.append(name)
        return FakeMove()


class FakeRobot:
    def __init__(self) -> None:
        self.targets: list[dict[str, object]] = []
        self.head_samples: list[object] = []
        self.body_samples: list[float] = []
        self.antenna_samples: list[list[float]] = []
        self.cancellations = 0

    def get_current_head_pose(self) -> list[list[float]]:
        return [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

    def goto_target(self, **kwargs: object) -> None:
        self.targets.append(kwargs)

    def set_target_head_pose(self, head: object) -> None:
        self.head_samples.append(head)

    def set_target_body_yaw(self, body_yaw: float) -> None:
        self.body_samples.append(body_yaw)

    def set_target_antenna_joint_positions(self, positions: list[float]) -> None:
        self.antenna_samples.append(positions)

    def cancel_move(self) -> None:
        self.cancellations += 1


def test_completed_robot_tool_call_requires_completed_output_item() -> None:
    payload = {
        "item": {
            "type": "function_call",
            "name": "move_reachy_head",
            "status": "completed",
            "call_id": "call-look",
            "arguments": '{"direction":"left"}',
        }
    }

    call = completed_robot_tool_call("response.output_item.done", payload)

    assert call is not None
    assert call.call_id == "call-look"
    assert call.name == "move_reachy_head"
    assert call.arguments == {"direction": "left"}
    assert completed_robot_tool_call("response.function_call_arguments.done", payload) is None


def test_manual_robot_controls_translate_only_curated_values() -> None:
    assert manual_robot_action("look", "left") == ("move_reachy_head", {"direction": "left"})
    assert manual_robot_action("emotion", "happy") == (
        "express_reachy_emotion",
        {"emotion": "happy"},
    )
    assert manual_robot_action("dance", "groovy") == ("dance_reachy", {"style": "groovy"})
    assert robot_control_options()["look"] == [
        "left",
        "right",
        "up",
        "down",
        "up_left",
        "up_right",
        "down_left",
        "down_right",
        "center",
    ]
    with pytest.raises(ValueError):
        manual_robot_action("joint", "neck=180")


def test_precision_controls_validate_only_cartesian_axes() -> None:
    assert manual_precision_action("pitch", -1.0, body_yaw_degrees=3.0) == (
        "nudge_reachy",
        {"axis": "pitch", "delta": -1.0, "body_yaw_degrees": 3.0},
    )
    assert manual_precision_action("center_all", 0.0) == (
        "nudge_reachy",
        {"axis": "center_all", "delta": 0.0, "body_yaw_degrees": 0.0},
    )
    assert manual_precision_action("body_yaw", 60.0, body_yaw_degrees=30.0) == (
        "nudge_reachy",
        {"axis": "body_yaw", "delta": 60.0, "body_yaw_degrees": 30.0},
    )
    with pytest.raises(ValueError):
        manual_precision_action("joint_4", 1.0)
    with pytest.raises(ValueError):
        manual_precision_action("yaw", 25.0)
    with pytest.raises(ValueError):
        manual_precision_action("body_yaw", 61.0)
    with pytest.raises(ValueError):
        manual_precision_action("body_yaw", float("nan"))
    with pytest.raises(ValueError):
        manual_precision_action("body_yaw", 5.0, body_yaw_degrees=float("nan"))


def test_precision_head_and_base_moves_are_clamped_and_interpolated(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        "homebody.robot_tools.interpolate_head_pose",
        lambda start, target, ratio: target,
    )
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    pitch = actions.execute(
        "nudge_reachy",
        {"axis": "pitch", "delta": -2.5, "body_yaw_degrees": 0.0},
    )
    pitch_head_sample = robot.head_samples[-1]
    base = actions.execute(
        "nudge_reachy",
        {"axis": "body_yaw", "delta": 60.0, "body_yaw_degrees": 100.0},
    )

    assert pitch["ok"] is True
    assert pitch["target"]["pitch"] == -2.5
    assert pitch_head_sample["pitch"] == -2.5
    assert pitch_head_sample["mm"] is True
    base_target = cast(dict[str, float], base["target"])
    assert base_target["body_yaw"] == 120.0
    assert base_target["yaw"] == 55.0
    assert cast(dict[str, float], robot.head_samples[-1])["yaw"] == 55.0
    assert base_target["body_yaw"] - base_target["yaw"] == 65.0
    assert robot.body_samples[-1] == pytest.approx(2.0943951024)


def test_precision_center_all_resets_head_and_base(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        "homebody.robot_tools.interpolate_head_pose",
        lambda start, target, ratio: target,
    )
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    result = actions.execute(
        "nudge_reachy",
        {"axis": "center_all", "delta": 0.0, "body_yaw_degrees": 18.0},
    )

    assert result["ok"] is True
    assert result["target"] == {
        "body_yaw": 0.0,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": 0.0,
    }
    assert robot.head_samples[-1]["pitch"] == 0.0
    assert robot.body_samples[-1] == 0.0


def test_presence_acknowledgement_is_a_small_head_only_action(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        "homebody.robot_tools.interpolate_head_pose",
        lambda start, target, ratio: target,
    )
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    result = actions.execute("acknowledge_presence", {"direction_degrees": 60.0})

    assert result == {
        "ok": True,
        "action": "acknowledge_presence",
        "target": {"pitch": -3.0, "yaw": 18.0},
    }
    assert cast(dict[str, float], robot.head_samples[-1])["pitch"] == -3.0
    assert cast(dict[str, float], robot.head_samples[-1])["yaw"] == 18.0
    assert robot.body_samples == []
    assert robot.antenna_samples == []
    assert robot.targets == []

    for invalid in (True, "left", 61.0, float("nan")):
        rejected = actions.execute("acknowledge_presence", {"direction_degrees": invalid})
        assert rejected["ok"] is False


def test_presence_acknowledgement_queue_is_cancellable(monkeypatch) -> None:
    robot = FakeRobot()
    results: list[dict[str, object]] = []
    actions = ReachyRobotActions(
        robot,
        threading.Event(),
        library_factory=FakeLibrary,
        on_result=lambda _name, result: results.append(result),
    )

    def wait_for_cancel(**_kwargs: object) -> bool:
        return not actions._cancel_requested.wait(1.0)

    monkeypatch.setattr(actions, "_run_precision_interpolation", wait_for_cancel)
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    actions.start()
    try:
        queued = actions.enqueue(
            "acknowledge_presence",
            {},
            hold_pose=True,
            reject_if_busy=True,
        )
        assert queued["accepted"] is True
        deadline = time.monotonic() + 1.0
        while not actions.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert actions.busy is True

        actions.cancel(stop_media=False)

        assert actions.wait_idle(timeout=1.0) is True
        assert results == [
            {
                "ok": False,
                "error": "Robot action was cancelled",
                "action": "acknowledge_presence",
            }
        ]
    finally:
        actions.close()


def test_camera_joystick_stream_rotates_head_and_base_together_at_interactive_rate(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)
    stream = CameraJoystickStream(watchdog_seconds=1.0)
    stream.update(1.0, 1.0)
    result: dict[str, object] = {}

    worker = threading.Thread(
        target=lambda: result.update(
            actions.execute(
                "camera_joystick",
                {"stream": stream, "body_yaw_degrees": 0.0},
            )
        )
    )
    worker.start()
    time.sleep(0.25)
    stream.stop(hold_body_yaw_degrees=0.0)
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert result == {"ok": True, "cancelled": True, "action": "camera_joystick"}
    moving_body_samples = [math.degrees(value) for value in robot.body_samples if value > 0]
    moving_head_samples = [cast(dict[str, float], value) for value in robot.head_samples if isinstance(value, dict)]
    assert len(moving_body_samples) >= 3
    assert moving_body_samples[-1] >= 4.0
    assert moving_head_samples[-1]["yaw"] == pytest.approx(moving_body_samples[-1], abs=0.2)
    assert moving_head_samples[-1]["pitch"] >= 3.0


def test_camera_joystick_stream_stops_when_browser_updates_expire(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)
    stream = CameraJoystickStream(watchdog_seconds=0.08)
    stream.update(1.0, 0.0)

    started = time.monotonic()
    result = actions.execute("camera_joystick", {"stream": stream, "body_yaw_degrees": 0.0})

    assert time.monotonic() - started < 0.4
    assert result == {"ok": True, "cancelled": True, "action": "camera_joystick"}
    assert robot.body_samples
    with pytest.raises(RuntimeError, match="not active"):
        stream.update(0.5, 0.0)


def test_camera_joystick_stream_stops_cleanly_through_action_queue(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    robot = FakeRobot()
    results: list[dict[str, object]] = []
    actions = ReachyRobotActions(
        robot,
        threading.Event(),
        library_factory=FakeLibrary,
        on_result=lambda _name, result: results.append(result),
    )
    stream = CameraJoystickStream(watchdog_seconds=1.0)
    stream.update(1.0, 0.0)
    actions.start()
    try:
        queued = actions.enqueue(
            "camera_joystick",
            {"stream": stream, "body_yaw_degrees": 0.0},
            hold_pose=True,
            reject_if_busy=True,
        )
        assert queued["accepted"] is True
        deadline = time.monotonic() + 1.0
        while not actions.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert actions.busy is True

        stream.stop(hold_body_yaw_degrees=0.0)
        actions.cancel(stop_media=False)

        assert actions.wait_idle(timeout=1.0) is True
        assert results == [{"ok": True, "cancelled": True, "action": "camera_joystick"}]
        assert robot.body_samples
    finally:
        actions.close()


def test_camera_joystick_is_allowlisted_at_the_action_queue_boundary() -> None:
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    result = actions.enqueue(
        "camera_joystick",
        {"pan": 0.25, "tilt": 0.0, "body_yaw_degrees": 0.0},
        hold_pose=True,
        reject_if_busy=True,
    )

    assert result["accepted"] is True
    assert actions.pending_count == 1
    actions.cancel(stop_media=False)
    assert actions.pending_count == 0


def test_camera_joystick_release_is_reported_as_expected_cancellation(monkeypatch) -> None:
    robot = FakeRobot()
    results: list[dict[str, object]] = []
    actions = ReachyRobotActions(
        robot,
        threading.Event(),
        library_factory=FakeLibrary,
        on_result=lambda _name, result: results.append(result),
    )

    def wait_for_release(**_kwargs: object) -> bool:
        return not actions._cancel_requested.wait(1.0)

    monkeypatch.setattr(actions, "_run_precision_interpolation", wait_for_release)
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    actions.start()
    try:
        queued = actions.enqueue(
            "camera_joystick",
            {"pan": 0.25, "tilt": 0.0, "body_yaw_degrees": 0.0},
            hold_pose=True,
            reject_if_busy=True,
        )
        assert queued["accepted"] is True
        deadline = time.monotonic() + 1.0
        while not actions.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert actions.busy is True

        actions.cancel(stop_media=False)
        assert actions.wait_idle(timeout=1.0) is True
        assert results == [{"ok": True, "cancelled": True, "action": "camera_joystick"}]
    finally:
        actions.close()


def test_camera_joystick_adds_bounded_base_assistance_near_head_limit(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        "homebody.robot_tools.interpolate_head_pose",
        lambda start, target, ratio: target,
    )
    monkeypatch.setattr(
        "homebody.robot_tools._head_pose_components",
        lambda _pose: {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 30.0},
    )
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    result = actions.execute(
        "camera_joystick",
        {"pan": 1.0, "tilt": 0.0, "body_yaw_degrees": 0.0},
    )

    target = cast(dict[str, float], result["target"])
    assert target == {"pitch": 0.0, "yaw": 33.0, "body_yaw": 2.0}
    assert target["yaw"] - target["body_yaw"] == 31.0
    assert robot.body_samples[-1] == pytest.approx(0.034906585)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -float("inf"), 1.01, -1.01])
def test_camera_joystick_rejects_non_finite_or_unbounded_input(value: object) -> None:
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    result = actions.execute(
        "camera_joystick",
        {"pan": value, "tilt": 0.0, "body_yaw_degrees": 0.0},
    )

    assert result == {"ok": False, "error": "Camera joystick input must be finite and bounded"}
    assert robot.head_samples == []
    assert robot.body_samples == []


def test_robot_actions_use_safe_curated_moves_without_move_audio(monkeypatch) -> None:
    monkeypatch.setattr(
        "homebody.robot_tools.create_head_pose",
        lambda **kwargs: kwargs,
    )
    robot = FakeRobot()
    library = FakeLibrary()
    actions = ReachyRobotActions(
        robot,
        threading.Event(),
        library_factory=lambda: library,
    )

    assert actions.execute("move_reachy_head", {"direction": "left"})["ok"] is True
    assert actions.execute("express_reachy_emotion", {"emotion": "happy"})["move"] == "laughing2"
    assert actions.execute("dance_reachy", {"style": "short"})["move"] == "dance1"

    assert robot.targets[0]["body_yaw"] is None
    assert library.requested == ["laughing2", "dance1"]
    assert robot.head_samples
    assert robot.body_samples
    assert robot.antenna_samples
    assert robot.cancellations == 0


def test_diagonal_look_uses_one_bounded_semantic_head_pose(monkeypatch) -> None:
    monkeypatch.setattr(
        "homebody.robot_tools.create_head_pose",
        lambda **kwargs: kwargs,
    )
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)

    result = actions.execute("move_reachy_head", {"direction": "up_left"})

    assert result == {"ok": True, "action": "move_reachy_head", "direction": "up_left"}
    assert robot.targets == [
        {
            "head": {"yaw": 25.0, "pitch": -18.0, "degrees": True},
            "antennas": None,
            "body_yaw": None,
            "duration": 0.6,
        }
    ]


def test_stop_cancels_precision_body_interpolation_without_late_targets(monkeypatch) -> None:
    robot = FakeRobot()
    completed = threading.Event()
    results: list[dict[str, object]] = []
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)
    actions.start()
    def on_complete(result: dict[str, object]) -> None:
        results.append(result)
        completed.set()

    actions.enqueue(
        "nudge_reachy",
        {"axis": "body_yaw", "delta": 10.0, "body_yaw_degrees": 0.0},
        on_complete=on_complete,
    )
    deadline = time.monotonic() + 1
    while not robot.body_samples and time.monotonic() < deadline:
        time.sleep(0.01)

    assert actions.cancel(stop_media=False) is True
    assert completed.wait(1)
    time.sleep(0.05)
    samples_after_stop = len(robot.body_samples)
    time.sleep(0.15)

    assert len(robot.body_samples) == samples_after_stop
    assert results == [{"ok": False, "error": "Robot action was cancelled", "action": "nudge_reachy"}]
    assert actions.wait_idle(1)
    actions.close()


def test_unknown_or_unapproved_robot_action_is_rejected() -> None:
    actions = ReachyRobotActions(FakeRobot(), threading.Event(), library_factory=FakeLibrary)

    assert actions.execute("dance_reachy", {"style": "unknown"})["ok"] is False
    assert actions.execute("shell", {"command": "rm -rf /"})["ok"] is False


def test_worker_completes_tool_with_actual_execution_result(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    robot = FakeRobot()
    completed = threading.Event()
    results: list[dict[str, object]] = []
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)
    actions.start()

    def on_complete(result: dict[str, object]) -> None:
        results.append(result)
        completed.set()

    accepted = actions.enqueue(
        "move_reachy_head",
        {"direction": "left"},
        on_complete=on_complete,
    )

    assert accepted == {"accepted": True, "queued": "move_reachy_head"}
    assert completed.wait(2)
    assert results == [{"ok": True, "action": "move_reachy_head", "direction": "left"}]
    actions.close()


def test_manual_actions_reject_surprising_queueing_when_robot_is_busy() -> None:
    actions = ReachyRobotActions(FakeRobot(), threading.Event(), library_factory=FakeLibrary)

    first = actions.enqueue("dance_reachy", {"style": "short"}, reject_if_busy=True)
    second = actions.enqueue("move_reachy_head", {"direction": "left"}, reject_if_busy=True)

    assert first["accepted"] is True
    assert second == {"ok": False, "error": "Robot is busy", "code": "robot_busy"}
    assert actions.pending_count == 1
    assert actions.cancel() is False
    assert actions.pending_count == 0


def test_manual_stop_uses_cooperative_move_flag_without_stopping_media() -> None:
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)
    actions._busy.set()

    assert actions.cancel(stop_media=False) is True
    assert actions._cancel_requested.is_set()
    assert robot.head_samples[-1] == robot.get_current_head_pose()
    assert robot.cancellations == 0


def test_manual_stop_interrupts_recorded_move_without_sdk_media_cancel() -> None:
    class LongMove(FakeMove):
        duration = 2.0

    class LongLibrary(FakeLibrary):
        def get(self, name: str) -> LongMove:
            self.requested.append(name)
            return LongMove()

    robot = FakeRobot()
    completed = threading.Event()
    results: list[dict[str, object]] = []
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=LongLibrary)
    actions.start()

    def on_complete(result: dict[str, object]) -> None:
        results.append(result)
        completed.set()

    actions.enqueue(
        "dance_reachy",
        {"style": "energetic"},
        on_complete=on_complete,
    )
    for _ in range(100):
        if robot.head_samples:
            break
        threading.Event().wait(0.01)

    assert actions.cancel(stop_media=False) is True
    assert completed.wait(1)
    assert results == [{"ok": False, "error": "Robot action was cancelled", "action": "dance_reachy"}]
    assert robot.cancellations == 0
    assert actions.wait_idle(1)
    actions.close()


def test_manual_look_can_hold_pose_without_restoring_idle_motion(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    completed = threading.Event()
    lifecycle: list[str] = []
    actions = ReachyRobotActions(
        FakeRobot(),
        threading.Event(),
        before_action=lambda: lifecycle.append("before"),
        after_action=lambda: lifecycle.append("after"),
    )
    actions.start()
    actions.enqueue(
        "move_reachy_head",
        {"direction": "left"},
        hold_pose=True,
        on_complete=lambda result: completed.set(),
    )

    assert completed.wait(2)
    assert lifecycle == ["before"]
    actions.close()


def test_cancel_generation_blocks_dequeued_action_before_execution(monkeypatch) -> None:
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    robot = FakeRobot()
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    results: list[dict[str, object]] = []

    def before_action() -> None:
        entered.set()
        release.wait(2)

    def on_complete(result: dict[str, object]) -> None:
        results.append(result)
        completed.set()

    lifecycle: list[str] = []
    actions = ReachyRobotActions(
        robot,
        threading.Event(),
        before_action=before_action,
        after_action=lambda: lifecycle.append("after"),
    )
    actions.start()
    actions.enqueue(
        "move_reachy_head",
        {"direction": "left"},
        on_complete=on_complete,
    )
    assert entered.wait(2)
    assert actions.cancel() is True
    release.set()

    assert completed.wait(2)
    assert results[0]["ok"] is False
    assert robot.targets == []
    assert robot.cancellations == 1
    assert lifecycle == []
    actions.close()
