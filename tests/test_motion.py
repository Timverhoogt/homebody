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


def test_sleep_stops_playback_before_disabling_wobble_and_fences_late_cues() -> None:
    robot = FakeRobot()
    motion = VoiceMotion(robot)
    motion.speaking()
    stopped = []

    def stop_playback():
        assert robot.wobbling is True
        stopped.append(True)

    motion.prepare_sleep(stop_playback)
    assert stopped == [True]
    assert robot.wobbling_events == [True, False]
    targets = list(robot.targets)
    motion.resume()  # A stale action's finally block must not undo the power fence.
    for cue in [motion.speaking, motion.idle, motion.listening, motion.thinking, motion.error]:
        cue()
    assert robot.targets == targets
    assert robot.wobbling_events == [True, False]

    motion.awake()  # Only a confirmed new physical wake re-admits voice cues.
    motion.speaking()
    assert robot.wobbling_events == [True, False, True]


def test_failed_playback_stop_does_not_clear_wobbler_with_live_callbacks() -> None:
    import pytest

    robot = FakeRobot()
    motion = VoiceMotion(robot)
    motion.speaking()

    def blocked_stop():
        raise RuntimeError("pipeline still live")

    with pytest.raises(RuntimeError, match="pipeline still live"):
        motion.prepare_sleep(blocked_stop)
    motion.resume()
    motion.speaking()
    assert robot.wobbling_events == [True]


def test_sleep_waits_for_inflight_pose_then_prevents_every_later_pose() -> None:
    import threading

    robot = FakeRobot()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def held_pose(**kwargs):
        entered.set()
        assert release.wait(2)

    robot.goto_target = held_pose
    motion = VoiceMotion(robot)
    voice = threading.Thread(target=motion.speaking)
    voice.start()
    assert entered.wait(2)
    sleep = threading.Thread(target=lambda: motion.prepare_sleep(stopped.set))
    sleep.start()
    assert not stopped.wait(0.05)
    release.set()
    voice.join(2)
    sleep.join(2)
    assert not voice.is_alive() and not sleep.is_alive()
    assert stopped.is_set()
    assert motion._wobbling is False
    motion.resume()
    motion.speaking()
    assert motion._wobbling is False
