"""Invalid measured poses must never become physical targets; fake hardware only."""
from __future__ import annotations

import threading

import pytest
from test_robot_tools import FakeRobot

from homebody.robot_tools import CameraJoystickStream, ReachyRobotActions, _head_pose_components


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("cell", [(r, c) for r in range(4) for c in range(4)])
def test_pose_extraction_rejects_every_nonfinite_matrix_cell(value, cell):
    pose = FakeRobot().get_current_head_pose()
    pose[cell[0]][cell[1]] = value
    with pytest.raises(RuntimeError, match="measured head pose"):
        _head_pose_components(pose)


@pytest.mark.parametrize("pose", [None, [], [[1.0]], [[0.0] * 4] * 3, [[0.0] * 3] * 4])
def test_pose_extraction_rejects_incomplete_matrices(pose):
    with pytest.raises(RuntimeError, match="measured head pose"):
        _head_pose_components(pose)


@pytest.mark.parametrize("action", ["stream", "step", "center", "hold", "cancel"])
def test_invalid_measured_pose_never_publishes_a_target(monkeypatch, action):
    robot = FakeRobot()
    pose = robot.get_current_head_pose()
    pose[2][0] = float("nan")
    monkeypatch.setattr(robot, "get_current_head_pose", lambda: pose)
    actions = ReachyRobotActions(robot, threading.Event())
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr("homebody.robot_tools.interpolate_head_pose", lambda start, target, ratio: target)
    if action == "cancel":
        actions._busy.set()
        actions.cancel(stop_media=False)
    else:
        with pytest.raises(RuntimeError, match="measured head pose"):
            if action == "stream":
                actions.execute("camera_joystick", {"stream": CameraJoystickStream(), "body_yaw_degrees": 0.0})
            elif action == "step":
                actions.execute("camera_joystick", {"pan": 0.0, "tilt": 0.0, "body_yaw_degrees": 0.0})
            elif action == "center":
                actions.execute("nudge_reachy", {"axis": "center_all", "delta": 0.0, "body_yaw_degrees": 0.0})
            else:
                actions._run_precision_interpolation(
                    target_head={"pitch": 0.0}, target_body=0.0, start_body=0.0, duration=.01,
                )
    assert robot.head_samples == []
    assert robot.body_samples == []
    assert robot.targets == []


def test_stream_cancellation_does_not_forward_invalid_hold_pose(monkeypatch):
    robot = FakeRobot()
    good = robot.get_current_head_pose()
    bad = robot.get_current_head_pose()
    bad[0][3] = float("nan")
    poses = iter([good, bad])
    monkeypatch.setattr(robot, "get_current_head_pose", lambda: next(poses))
    actions = ReachyRobotActions(robot, threading.Event())
    stream = CameraJoystickStream()
    stream.stop()
    with pytest.raises(RuntimeError, match="measured head pose"):
        actions.execute("camera_joystick", {"stream": stream, "body_yaw_degrees": 0.0})
    assert robot.head_samples == []
    assert robot.body_samples == []
