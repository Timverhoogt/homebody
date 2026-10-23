"""Small, safe motions used as voice-state indicators."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
from reachy_mini.utils import create_head_pose

from .playback_gate import PlaybackGate

_LOGGER = logging.getLogger(__name__)


class VoiceRobot(Protocol):
    """Reachy motion methods used by the voice-state animator."""

    def goto_target(self, **kwargs: object) -> object: ...

    def enable_wobbling(self) -> None: ...

    def disable_wobbling(self) -> None: ...

    media: Any
    client: Any


class VoiceMotion:
    """Express voice state without a competing real-time motor loop."""

    def __init__(self, robot: VoiceRobot, *, enabled: bool = True, playback: PlaybackGate | None = None) -> None:
        self.robot = robot
        self.enabled = enabled
        self._wobbling = False
        self._suspended = False
        self._power_suspended = False
        self._lock = playback.lock if playback is not None else threading.RLock()
        self._playback = playback
        self._wobble_generation = 0

    def suspend(self) -> None:
        """Yield the head to an explicit robot action while preserving voice audio."""
        with self._lock:
            self._set_wobbling(False)
            self._suspended = True

    def resume(self) -> None:
        with self._lock:
            self._suspended = False

    def prepare_sleep(self, stop_playback: Callable[[], None]) -> None:
        """Fence late cues, stop playback, then safely remove its wobbler.

        SDK 1.9 dereferences the wobbler twice in its GStreamer callback. Do not
        clear it until stop_playing has stopped the playback pipelines. Keep a
        separate power fence: an old action's resume() must not undo Sleep.
        """
        with self._lock:
            self._power_suspended = True
            self._wobble_generation += 1
        # SDK pipeline teardown may wait for a callback on another thread. That callback
        # must be able to acquire our lock, observe the fence and return; holding it here
        # creates a teardown/callback lock cycle. The runtime serializes power transitions.
        stop_playback()
        if self._playback is not None:
            self._playback.confirm_stopped()
        self.close()

    def awake(self) -> None:
        """Re-admit voice cues only after the runtime confirms a new wake."""
        with self._lock:
            self._power_suspended = False
            self._suspended = False

    def fence_motor_recovery(self) -> None:
        """Retire old cues locally, without sending a pose or resetting media."""
        with self._lock:
            self._power_suspended = True
            self._suspended = True
            self._wobble_generation += 1
            # Keep the SDK callback non-null; the next fresh speaking generation
            # replaces it instead of reusing the retired callback's lease.
            self._wobbling = False

    def _set_wobbling(self, enabled: bool) -> None:
        """Enable once; disable only after playback teardown, not between cues."""
        with self._lock:
            if (
                not self.enabled
                or self._suspended
                or self._power_suspended
                or enabled == self._wobbling
                or (self._playback is not None and self._playback.blocked)
            ):
                return
            # SDK 1.9's GStreamer callback reads the wobbler concurrently.
            # Clearing it during live playback can make its second read None.
            # Sleep and shutdown explicitly quiesce playback before close().
            if not enabled:
                return
            try:
                from reachy_mini.io.protocol import SetWobblingCmd

                # Use the public media API with an emission fence, not the
                # unguarded closure in ReachyMini.enable_wobbling().
                generation = self._wobble_generation
                self.robot.media.enable_wobbling(lambda offsets: self._emit_offsets(offsets, generation))
                self._wobbling = True
                self.robot.client.send_command(SetWobblingCmd(enabled=True))
            except Exception as exc:
                _LOGGER.warning("Could not change Reachy voice wobbling: %s", exc)

    def _emit_offsets(self, offsets: tuple[float, ...], generation: int) -> None:
        """Fence even an SDK callback that already passed HeadWobbler._fire's check."""
        from reachy_mini.io.protocol import SetSpeechOffsetsCmd

        with self._lock:
            if (
                self._power_suspended
                or generation != self._wobble_generation
                or (self._playback is not None and self._playback.blocked)
            ):
                return
            try:
                self.robot.client.send_command(SetSpeechOffsetsCmd(offsets=list(offsets)))
            except ConnectionError:
                pass  # Never retry an offset after losing ownership.

    def close(self) -> None:
        """Disable wobbling after the owner has stopped all audio callbacks."""
        with self._lock:
            self._wobble_generation += 1
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
        with self._lock:
            if (
                not self.enabled
                or self._suspended
                or self._power_suspended
                or (self._playback is not None and self._playback.blocked)
            ):
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
