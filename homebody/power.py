"""Power modes and motor torque for the voice runtime: Standby, Awake, timed Meeting, and Sleep.

``PowerMixin`` is mixed into ``HermesVoiceRuntime``. Every transition is serialized by the runtime's
``_motor_transition_lock`` and folds Reachy safely before torque is released.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import httpx

from .safety_gate import POWER_MODES as _POWER_MODES
from .wakeword import WAKE_PROMPT as _WAKE_PROMPT

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")

class PowerMixin:
    """Serialized power transitions, safe fold before torque release, and confirmed wake."""

    def _init_power_state(self) -> None:
        self._power_lock = threading.RLock()
        self._motors_enabled: bool | None = None
        self._head_safely_folded = False
        self._power_mode = "standby"
        self._meeting_until = 0.0

    def set_power_mode(
        self,
        mode: str,
        *,
        duration_seconds: float = 0.0,
        cancel_announcements: bool = True,
    ) -> dict[str, object]:
        """Change the voice lifecycle as one serialized, hardware-checked transition."""
        if self._runtime_started and not self._control_ready.is_set():
            raise RuntimeError("Voice runtime is still starting; no power transition was attempted")
        mode = mode.strip().lower()
        if mode not in _POWER_MODES:
            raise ValueError(f"Unsupported power mode: {mode}")
        if mode != "awake":
            self.stop_presentation_window(f"power_{mode}")
        self.revoke_camera_control()
        if mode in {"standby", "meeting", "sleep"}:
            self.cancel_agent_work(f"power_{mode}")
        if cancel_announcements and mode in {"standby", "meeting", "sleep"}:
            self._cancel_announcements(clear_queue=mode in {"meeting", "sleep"})
            if mode in {"meeting", "sleep"}:
                with self._status_lock:
                    self._status.announcement_current_preview = ""
                    self._status.announcement_last_text = ""
        with self._motor_transition_lock:
            if mode in {"standby", "meeting", "sleep"}:
                self._voice_workspace.clear()
                self._request_conversation_stop()
            else:
                self._conversation_stop_requested.clear()
            if mode in {"meeting", "sleep"}:
                self._privacy_requested.set()
            with self._power_lock:
                self._power_mode = mode
                self._meeting_until = (
                    time.monotonic() + max(60.0, min(duration_seconds, 8 * 3600.0)) if mode == "meeting" else 0.0
                )
            try:
                self._apply_power_mode()
            finally:
                if mode not in {"meeting", "sleep"}:
                    self._privacy_requested.clear()
            status = self.status()
            transition_error = str(status.get("last_error") or "")
            if transition_error:
                raise RuntimeError(transition_error)
            return status

    def _effective_power_mode(self) -> str:
        with self._power_lock:
            if self._power_mode == "meeting" and time.monotonic() >= self._meeting_until:
                self._power_mode = "standby"
                self._meeting_until = 0.0
                self._privacy_requested.clear()
            return self._power_mode

    def _read_head_safely_folded(self) -> bool:
        """Read whether the current pose is already inside the supported sleep cradle."""
        try:
            response = httpx.get("http://127.0.0.1:8000/api/state/full", timeout=2.0)
            response.raise_for_status()
            pose = response.json().get("head_pose") or {}
            return float(pose.get("z", 0.0)) <= -0.03 and float(pose.get("pitch", 0.0)) >= 0.25
        except Exception as exc:
            _LOGGER.warning("Could not verify whether Reachy's head is folded: %s", exc)
            return False

    def _fold_head_before_torque_release(self) -> tuple[bool, str]:
        """Fold Reachy into its supported pose before releasing motor torque."""
        with self._motor_transition_lock:
            if self._head_safely_folded and self._read_head_safely_folded():
                try:
                    self._set_motor_mode(False)
                except RuntimeError as exc:
                    message = f"Reachy is folded, but disabling motor torque failed: {exc}"
                    _LOGGER.error(message)
                    return False, message
                return True, ""
            self._head_safely_folded = False
            if self._actions is not None:
                self._actions.cancel(stop_media=False)
                if not self._actions.wait_idle(timeout=5.0):
                    message = "Robot movement did not stop; motors remain enabled to prevent an unsafe fold"
                    _LOGGER.error(message)
                    try:
                        self._set_motor_mode(True)
                    except RuntimeError:
                        _LOGGER.exception("Could not preserve torque after movement stop timeout")
                    return False, message
            if self._playback_stopped_for_privacy:
                start_playing = getattr(self.robot.media, "start_playing", None)
                if callable(start_playing):
                    start_playing()
                self._playback_stopped_for_privacy = False
            try:
                self._set_motor_mode(True)
            except RuntimeError as exc:
                message = f"Could not enable motor torque for safe folding; torque state is unverified: {exc}"
                _LOGGER.error(message)
                return False, message
            goto_sleep = getattr(self.robot, "goto_sleep", None)
            if not callable(goto_sleep):
                message = "Reachy SDK sleep movement is unavailable; motors remain enabled to prevent a head drop"
                _LOGGER.error(message)
                return False, message
            try:
                goto_sleep()
            except Exception as exc:
                message = f"Reachy sleep movement failed; motors remain enabled to prevent a head drop: {exc}"
                _LOGGER.exception("Could not run Reachy's native sleep movement")
                try:
                    self._set_motor_mode(True)
                except RuntimeError:
                    _LOGGER.exception("Could not reconfirm enabled torque after sleep movement failure")
                return False, message
            if not self._read_head_safely_folded():
                message = "Reachy sleep movement returned without a verified folded pose; motors remain enabled"
                _LOGGER.error(message)
                try:
                    self._set_motor_mode(True)
                except RuntimeError:
                    _LOGGER.exception("Could not preserve torque after unverified sleep pose")
                return False, message
            self._head_safely_folded = True
            try:
                self._set_motor_mode(False)
            except RuntimeError as exc:
                message = f"Reachy folded safely, but disabling motor torque failed: {exc}"
                _LOGGER.error(message)
                return False, message
            _LOGGER.info("Reachy completed its native sleep movement before torque release")
            return True, ""

    def _apply_power_mode(self) -> None:
        """Apply the selected mode without overlapping another motor transition."""
        with self._motor_transition_lock:
            self._apply_power_mode_unlocked()

    def _apply_power_mode_unlocked(self) -> None:
        mode = self._effective_power_mode()
        remaining = 0
        with self._power_lock:
            if mode == "meeting":
                remaining = max(0, int(self._meeting_until - time.monotonic()))
        if mode in {"meeting", "sleep"}:
            self._presence.clear(f"power_{mode}")
            self._face_tracking_desired = False
            self._set_face_tracking(False)
            if self._actions is not None:
                self._playback_stopped_for_privacy = self._actions.cancel() or self._playback_stopped_for_privacy
            try:
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
            except Exception:
                _LOGGER.debug("No active playback to stop", exc_info=True)
            self._clear_streamed_audio()
            if self._recording:
                try:
                    self.robot.media.stop_recording()
                finally:
                    self._recording = False
            head_folded, transition_error = self._fold_head_before_torque_release()
            if mode == "sleep":
                detail = (
                    "Voice is disabled; Reachy is folded safely into Sleep"
                    if head_folded
                    else "Voice is disabled; sleep motion failed and motor torque remains enabled"
                )
            else:
                detail = (
                    "Voice is disabled; Reachy is folded safely for Meeting"
                    if head_folded
                    else "Voice is disabled; safe motor release failed and torque remains enabled"
                )
            self._set_status(
                mode,
                detail,
                power_mode=mode,
                meeting_seconds_remaining=remaining,
                last_error=transition_error,
            )
            return
        if self._playback_stopped_for_privacy:
            start_playing = getattr(self.robot.media, "start_playing", None)
            if callable(start_playing):
                start_playing()
            self._playback_stopped_for_privacy = False
        if self._motion is not None:
            self._motion.resume()
        if not self._recording:
            self.robot.media.start_recording()
            self._recording = True
        if mode == "standby":
            head_folded, transition_error = self._fold_head_before_torque_release()
            self._set_status(
                "waiting_for_wake_word",
                (
                    "Local wake detection only; Reachy is folded safely"
                    if head_folded
                    else "Local wake detection active; safe motor release failed"
                ),
                power_mode=mode,
                meeting_seconds_remaining=0,
                last_error=transition_error,
            )
        else:
            try:
                self._set_motor_mode(True, wake=True)
            except RuntimeError as exc:
                folded, recovery_error = self._fold_head_before_torque_release()
                with self._power_lock:
                    self._power_mode = "standby"
                    self._meeting_until = 0.0
                self._request_conversation_stop()
                error = str(exc)
                if recovery_error:
                    error = f"{error}; automatic safe-Standby recovery also failed: {recovery_error}"
                self._set_status(
                    "power_transition_error",
                    (
                        "Awake failed; Reachy returned to folded Standby"
                        if folded
                        else "Awake failed; motor state requires attention"
                    ),
                    power_mode="standby",
                    meeting_seconds_remaining=0,
                    last_error=error,
                )
                return
            self._set_status(
                "waiting_for_wake_word",
                _WAKE_PROMPT,
                power_mode=mode,
                meeting_seconds_remaining=0,
                last_error="",
            )

    def _set_motor_mode(self, enabled: bool, *, wake: bool = False) -> None:
        """Change motor torque through the daemon's supported local API or fail explicitly."""
        mode = "enabled" if enabled else "disabled"
        try:
            response = httpx.post(f"http://127.0.0.1:8000/api/motors/set_mode/{mode}", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            message = f"Could not set Reachy motors to {mode}: {exc}"
            _LOGGER.warning(message)
            raise RuntimeError(message) from exc
        self._motors_enabled = enabled
        if enabled and wake:
            self._head_safely_folded = False
            try:
                self.robot.wake_up()
            except Exception as exc:
                if self._wake_pose_is_ready():
                    _LOGGER.warning(
                        "Reachy wake call raised after the measured head reached Awake; preserving enabled torque: %s",
                        exc,
                    )
                    return
                message = f"Reachy motor torque was enabled, but the wake motion failed: {exc}"
                _LOGGER.exception(message)
                raise RuntimeError(message) from exc
        _LOGGER.info("Reachy motors %s%s", mode, " with wake motion" if wake else "")

    def _wake_pose_is_ready(self) -> bool:
        """Confirm the native wake motion reached a clearly unfolded pose."""
        try:
            response = httpx.get("http://127.0.0.1:8000/api/state/full", timeout=2.0)
            response.raise_for_status()
            pose = response.json().get("head_pose") or {}
            z = float(pose["z"])
            pitch = float(pose["pitch"])
            return math.isfinite(z) and math.isfinite(pitch) and z > -0.02 and pitch < 0.20
        except Exception:
            return False
