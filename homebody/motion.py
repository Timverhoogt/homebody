"""Small, safe motions used as voice-state indicators."""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np
from reachy_mini.utils import create_head_pose

_LOGGER = logging.getLogger(__name__)


class VoiceRobot(Protocol):
    """Reachy motion methods used by the voice-state animator."""

    def goto_target(self, **kwargs: object) -> object: ...

    def enable_wobbling(self) -> None: ...

    def disable_wobbling(self) -> None: ...


class VoiceMotion:
    """Express voice state without a competing real-time motor loop."""

    def __init__(self, robot: VoiceRobot, *, enabled: bool = True) -> None:
        self.robot = robot
        self.enabled = enabled
        self._wobbling = False
        self._suspended = False

    def suspend(self) -> None:
        """Yield the head to an explicit robot action while preserving voice audio."""
        self._set_wobbling(False)
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    def _set_wobbling(self, enabled: bool) -> None:
        """Enable wobbling once; disable only after playback teardown in close()."""
        if not self.enabled or self._suspended or enabled == self._wobbling:
            return
        # reachy-mini 1.9's GStreamer callback reads the wobbler concurrently.
        # Clearing it while playback is live can turn the callback's checked
        # object into None before feed(). Silence naturally produces no offsets,
        # so keep the supported SDK wobbler alive until the pipeline is stopped.
        if not enabled:
            return
        try:
            self.robot.enable_wobbling()
            self._wobbling = True
        except Exception as exc:
            _LOGGER.warning("Could not change Reachy voice wobbling: %s", exc)

    def close(self) -> None:
        """Disable wobbling after the owner has stopped all audio callbacks."""
        if not self._wobbling:
            return
        self.robot.disable_wobbling()
        self._wobbling = False

    def _pose(
        self,
        *,
        pitch: float = 0.0,
        roll: float = 0.0,
        yaw: float = 0.0,
        right_antenna: float = 0.0,
        left_antenna: float = 0.0,
        duration: float = 0.35,
    ) -> None:
        if not self.enabled or self._suspended:
            return
        try:
            head = create_head_pose(pitch=pitch, roll=roll, yaw=yaw, degrees=True)
            antennas = np.deg2rad(np.asarray([right_antenna, left_antenna], dtype=np.float64))
            self.robot.goto_target(head=head, antennas=antennas, duration=duration)
        except Exception as exc:
            _LOGGER.warning("Could not apply Reachy voice pose: %s", exc)

    def listening(self) -> None:
        self._set_wobbling(False)
        self._pose(pitch=-5.0, yaw=5.0, right_antenna=18.0, left_antenna=-18.0)

    def orient_to_sound(self, yaw_degrees: float) -> None:
        """Briefly turn toward the locally measured wake-phrase direction."""
        self._set_wobbling(False)
        self._pose(yaw=yaw_degrees, right_antenna=12.0, left_antenna=-12.0, duration=0.5)

    def thinking(self) -> None:
        self._set_wobbling(False)
        self._pose(pitch=-2.0, roll=6.0, right_antenna=8.0, left_antenna=-22.0, duration=0.45)

    def speaking(self) -> None:
        self._pose(pitch=3.0, roll=-2.0, right_antenna=20.0, left_antenna=-20.0, duration=0.3)
        self._set_wobbling(True)

    def idle(self) -> None:
        self._set_wobbling(False)
        self._pose(duration=0.45)

    def error(self) -> None:
        self._set_wobbling(False)
        self._pose(pitch=5.0, roll=-7.0, right_antenna=-8.0, left_antenna=8.0, duration=0.35)
