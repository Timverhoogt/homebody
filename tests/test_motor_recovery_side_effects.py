"""Motor-loss fences must retire old work without emitting a stale native packet."""

import threading

from test_motion import FakeRobot as MotionRobot
from test_robot_tools import FakeRobot as ActionRobot

from homebody.motion import VoiceMotion
from homebody.robot_tools import ReachyRobotActions


def test_motor_recovery_retires_old_wobbler_and_cues_without_touching_live_media():
    robot = MotionRobot()
    motion = VoiceMotion(robot)
    motion.speaking()
    old = robot.offset_callbacks[-1]
    targets = list(robot.targets)
    events = list(robot.wobbling_events)
    motion.fence_motor_recovery()
    old((1, 2, 3, 4, 5, 6))
    motion.resume()  # Late action cleanup must not clear the power/recovery fence.
    motion.speaking()
    motion.idle()
    assert robot.targets == targets and robot.wobbling_events == events
    assert robot.offset_commands == []
    motion.awake()
    motion.speaking()
    fresh = robot.offset_callbacks[-1]
    assert fresh is not old
    old((1, 2, 3, 4, 5, 6))
    assert robot.offset_commands == []
    fresh((1, 2, 3, 4, 5, 6))
    assert robot.offset_commands


def test_lost_backend_cancel_discards_work_without_sending_freeze_or_stop():
    robot = ActionRobot()
    actions = ReachyRobotActions(robot, threading.Event())
    before = actions._generation
    actions._busy.set()
    try:
        assert actions.cancel(backend_available=False)
        assert actions._generation > before and actions._cancel_requested.is_set()
        assert robot.cancellations == 0 and not robot.targets and not robot.head_samples
    finally:
        actions._busy.clear()
        actions.close()
