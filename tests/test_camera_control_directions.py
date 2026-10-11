"""Camera-view directions must agree with Reachy's semantic look controls."""

import math
import threading

import pytest
from test_robot_tools import FakeLibrary, FakeRobot

from homebody.robot_tools import _LOOK_POSES, CameraJoystickStream, ReachyRobotActions


@pytest.mark.parametrize("streaming", [False, True], ids=["step", "stream"])
@pytest.mark.parametrize(
    ("direction", "pan", "tilt", "axis"),
    [
        ("up", 0.0, 0.55, "pitch"),
        ("down", 0.0, -0.55, "pitch"),
        ("right", 0.55, 0.0, "yaw"),
        ("left", -0.55, 0.0, "yaw"),
    ],
)
def test_camera_view_direction_matches_semantic_look(monkeypatch, streaming, direction, pan, tilt, axis):
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr("homebody.robot_tools.interpolate_head_pose", lambda start, target, ratio: target)
    robot = FakeRobot()
    actions = ReachyRobotActions(robot, threading.Event(), library_factory=FakeLibrary)
    arguments = {"pan": pan, "tilt": tilt, "body_yaw_degrees": 0.0}
    if streaming:
        stream = CameraJoystickStream(watchdog_seconds=0.12)
        stream.update(pan, tilt)
        arguments = {"stream": stream, "body_yaw_degrees": 0.0}
    try:
        result = actions.execute("camera_joystick", arguments)
        assert result["ok"] is True
        targets = [sample for sample in robot.head_samples if isinstance(sample, dict)]
        assert targets
        target = targets[-1]
        assert target[axis] * _LOOK_POSES[direction][axis] > 0, direction
        # SDK forward +X, left +Y, up +Z: positive pitch looks down.
        forward_y = math.sin(math.radians(target["yaw"]))
        forward_z = -math.sin(math.radians(target["pitch"]))
        if direction == "up":
            assert forward_z > 0
        elif direction == "down":
            assert forward_z < 0
        elif direction == "right":
            assert forward_y < 0
        else:
            assert forward_y > 0
        if streaming and pan:
            assert robot.body_samples[0] * _LOOK_POSES[direction][axis] > 0
            assert math.degrees(robot.body_samples[-2]) == pytest.approx(target["yaw"])
    finally:
        actions.close()
