"""Tests for Reachy's voice-state motion cues."""

import sys
from types import ModuleType

# Keep this unit test independent from Reachy's hardware SDK and its native
# media dependencies.
reachy_module = ModuleType("reachy_mini")
utils_module = ModuleType("reachy_mini.utils")
utils_module.create_head_pose = lambda **kwargs: kwargs  # type: ignore[attr-defined]
sys.modules.setdefault("reachy_mini", reachy_module)
sys.modules.setdefault("reachy_mini.utils", utils_module)

from homebody.motion import VoiceMotion  # noqa: E402


class FakeRobot:
    def __init__(self) -> None:
        self.targets: list[dict[str, object]] = []
        self.wobbling = False
        self.wobbling_events: list[bool] = []

    def goto_target(self, **kwargs: object) -> None:
        self.targets.append(kwargs)

    def enable_wobbling(self) -> None:
        self.wobbling = True
        self.wobbling_events.append(True)

    def disable_wobbling(self) -> None:
        self.wobbling = False
        self.wobbling_events.append(False)


def test_speaking_wobbler_stays_allocated_until_safe_close() -> None:
    robot = FakeRobot()
    motion = VoiceMotion(robot)

    motion.speaking()
    assert robot.wobbling is True
    assert robot.targets

    motion.idle()
    assert robot.wobbling is True
    assert robot.wobbling_events == [True]

    motion.close()
    assert robot.wobbling is False
    assert robot.wobbling_events == [True, False]


def test_interruption_does_not_disable_wobbler_while_audio_callbacks_are_live() -> None:
    robot = FakeRobot()
    motion = VoiceMotion(robot)

    motion.speaking()
    motion.listening()
    motion.thinking()
    motion.suspend()

    assert robot.wobbling_events == [True]


def test_disabled_motion_does_not_move_robot() -> None:
    robot = FakeRobot()
    motion = VoiceMotion(robot, enabled=False)

    motion.listening()
    motion.speaking()
    motion.idle()

    assert robot.targets == []
    assert robot.wobbling_events == []
