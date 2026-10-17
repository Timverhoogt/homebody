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

from .daemon_sleep import DaemonSleepError, run_daemon_sleep
from .safety_gate import POWER_MODES as _POWER_MODES
from .wakeword import WAKE_PROMPT as _WAKE_PROMPT

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")

class MotorBackendUnavailable(RuntimeError):
    """Motor command outcome cannot be trusted; do not issue recovery motion."""


class PowerMixin:
    """Serialized power transitions, safe fold before torque release, and confirmed wake."""

    def _init_power_state(self) -> None:
        self._power_lock = threading.RLock()
        self._motors_enabled: bool | None = None
        self._head_safely_folded = False
        self._power_mode = "standby"
        self._meeting_until = 0.0
        self._pending_privacy_transitions = 0
        self._native_sleep_uncertain = False

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
        privacy_transition = mode in {"meeting", "sleep"}
        if privacy_transition:
            # Publish revocation before waiting on an in-progress scan/wake. A second
            # privacy caller keeps this intent set even if another transition completes.
            with self._power_lock:
                self._pending_privacy_transitions += 1
                self._privacy_requested.set()
            self._request_conversation_stop()
        try:
            if mode != "awake":
                self.stop_presentation_window(f"power_{mode}")
            self.revoke_camera_control()
            if mode in {"standby", "meeting", "sleep"}:
                self.cancel_agent_work(f"power_{mode}")
            if cancel_announcements and mode in {"standby", "meeting", "sleep"}:
                self._cancel_announcements(clear_queue=privacy_transition)
                if privacy_transition:
                    with self._status_lock:
                        self._status.announcement_current_preview = ""
                        self._status.announcement_last_text = ""
            with self._motor_transition_lock:
                if mode == "awake":
                    with self._power_lock:
                        if self._pending_privacy_transitions:
                            raise RuntimeError("Awake was cancelled by a pending privacy transition")
                if mode in {"standby", "meeting", "sleep"}:
                    self._voice_workspace.clear()
                    self._request_conversation_stop()
                else:
                    self._conversation_stop_requested.clear()
                with self._power_lock:
                    self._power_mode = mode
                    self._meeting_until = (
                        time.monotonic() + max(60.0, min(duration_seconds, 8 * 3600.0))
                        if mode == "meeting" else 0.0
                    )
                self._apply_power_mode()
                # Public child status intentionally hides errors; it is not a control
                # receipt. Read the internal transition result before returning success.
                with self._status_lock:
                    transition_error = self._status.last_error
                if transition_error:
                    raise RuntimeError(transition_error)
                return self.status()
        finally:
            with self._power_lock:
                if privacy_transition:
                    self._pending_privacy_transitions -= 1
                if not self._pending_privacy_transitions and self._power_mode not in {"meeting", "sleep"}:
                    self._privacy_requested.clear()

    def _effective_power_mode(self) -> str:
        with self._power_lock:
            if self._power_mode == "meeting" and time.monotonic() >= self._meeting_until:
                self._power_mode = "standby"
                self._meeting_until = 0.0
                if not self._pending_privacy_transitions:
                    self._privacy_requested.clear()
            return self._power_mode

    def _motor_backend_unavailable(self, exc: Exception) -> MotorBackendUnavailable:
        """Invalidate cached confirmations; unknown hardware must not authorize motion."""
        self._motors_enabled = None
        self._head_safely_folded = False
        _LOGGER.warning("Motor command or measured state unavailable: %s", exc)
        return MotorBackendUnavailable(
            "Reachy's hardware connection is unavailable; motor torque and folded pose are unverified. "
            "No automatic folding or motor retry was attempted after the unavailable result. "
            "Check robot power and USB, then recover the Reachy daemon with the owner present "
            "and clear space before trying again."
        )

    def _read_head_safely_folded(self) -> bool:
        """Read whether the current pose is already inside the supported sleep cradle."""
        try:
            response = httpx.get("http://127.0.0.1:8000/api/state/full", timeout=2.0)
            response.raise_for_status()
            pose = response.json()["head_pose"]
            z, pitch = float(pose["z"]), float(pose["pitch"])
            if not math.isfinite(z) or not math.isfinite(pitch):
                raise ValueError("Folded-pose measurement is not finite")
            return z <= -0.03 and pitch >= 0.25
        except Exception as exc:
            raise self._motor_backend_unavailable(exc) from exc

    def _fold_head_before_torque_release(self) -> tuple[bool, str]:
        """Permit only serialized safety folding after shutdown, never a fresh wake."""
        with self._motor_transition_lock:
            prior = getattr(self, "_safety_fold_active", False)
            self._safety_fold_active = True
            try:
                return self._fold_head_before_torque_release_unlocked()
            finally:
                self._safety_fold_active = prior
                # stop_playing also stops SDK 1.9's shared capture pipeline.
                # Automatic return-to-Standby folds use this helper directly.
                if self._effective_power_mode() == "standby" and not self._recording:
                    self.robot.media.start_recording()
                    self._recording = True

    def _fold_head_before_torque_release_unlocked(self) -> tuple[bool, str]:
        """Fold Reachy into its supported pose before releasing motor torque."""
        with self._motor_transition_lock:
            if self._native_sleep_uncertain:
                return False, "Native sleep outcome is unknown; motor transitions require inspection before restart"
            # Quiesce the SDK-side producer before the daemon clears its own offsets.
            # The daemon, not the client, decides whether an already-folded head moves.
            try:
                self._playback.suspend()
                if self._motion is not None:
                    self._motion.prepare_sleep(self.robot.media.stop_playing)
                else:
                    # Absence of a motion helper is not proof that audio stopped.
                    self.robot.media.stop_playing()
                    self._playback.confirm_stopped()
                self._playback_stopped_for_privacy = True
                self._recording = False  # SDK 1.9 shares a capture/playback pipeline.
            except Exception as exc:
                return False, f"Could not stop voice wobbling before native sleep: {exc}"
            self._head_safely_folded = False
            if self._actions is not None:
                self._actions.cancel(stop_media=False)
                if not self._actions.wait_idle(timeout=5.0):
                    message = "Robot movement did not stop; motors remain enabled to prevent an unsafe fold"
                    _LOGGER.error(message)
                    try:
                        self._set_motor_mode(True)
                    except MotorBackendUnavailable as exc:
                        return False, str(exc)
                    except RuntimeError:
                        _LOGGER.exception("Could not preserve torque after movement stop timeout")
                    return False, message
            try:
                self._set_motor_mode(True)
            except RuntimeError as exc:
                message = f"Could not enable motor torque for safe folding; torque state is unverified: {exc}"
                _LOGGER.error(message)
                return False, message
            try:
                self._run_native_sleep()
            except Exception as exc:
                if isinstance(exc, DaemonSleepError) and exc.uncertain:
                    self._native_sleep_uncertain = True
                    self._motors_enabled = None
                    message = "Native sleep outcome is unknown; no further motor command was sent"
                    _LOGGER.exception(message)
                    return False, message
                message = f"Reachy sleep movement failed; motors remain enabled to prevent a head drop: {exc}"
                _LOGGER.exception("Could not run Reachy's native sleep movement")
                try:
                    self._set_motor_mode(True)
                except MotorBackendUnavailable as unavailable:
                    return False, str(unavailable)
                except RuntimeError:
                    _LOGGER.exception("Could not reconfirm enabled torque after sleep movement failure")
                return False, message
            try:
                measured_folded = self._read_head_safely_folded()
            except MotorBackendUnavailable as exc:
                return False, str(exc)
            if not measured_folded:
                message = "Reachy sleep movement returned without a verified folded pose; motors remain enabled"
                _LOGGER.error(message)
                try:
                    self._set_motor_mode(True)
                except MotorBackendUnavailable as exc:
                    return False, str(exc)
                except RuntimeError:
                    _LOGGER.exception("Could not preserve torque after unverified sleep pose")
                return False, message
            self._head_safely_folded = True
            try:
                self._set_motor_mode(False)
            except RuntimeError as exc:
                message = (
                    str(exc) if isinstance(exc, MotorBackendUnavailable)
                    else f"Reachy folded safely, but disabling motor torque failed: {exc}"
                )
                _LOGGER.error(message)
                return False, message
            _LOGGER.info("Reachy completed its native sleep movement before torque release")
            return True, ""

    def _run_native_sleep(self) -> None:
        """Use Reachy Control's daemon routine, including its exact cleanup/motion."""
        run_daemon_sleep()

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
                self._playback.play_sound(str(self.assets / "silence.wav"))
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
                    else (
                        "Voice is disabled; motor torque and folded pose are unverified"
                        if self._motors_enabled is None
                        else "Voice is disabled; sleep motion failed and motor torque remains enabled"
                    )
                )
            else:
                detail = (
                    "Voice is disabled; Reachy is folded safely for Meeting"
                    if head_folded
                    else (
                        "Voice is disabled; motor torque and folded pose are unverified"
                        if self._motors_enabled is None
                        else "Voice is disabled; safe motor release failed and torque remains enabled"
                    )
                )
            self._set_status(
                mode,
                detail,
                power_mode=mode,
                meeting_seconds_remaining=remaining,
                last_error=transition_error,
            )
            return
        if mode == "standby":
            head_folded, transition_error = self._fold_head_before_torque_release()
            if not self._recording:
                self.robot.media.start_recording()
                self._recording = True
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
                if isinstance(exc, MotorBackendUnavailable):
                    # The same dead backend cannot safely fold. In particular a
                    # timeout may have admitted the first write: never replay it.
                    folded, recovery_error = False, ""
                else:
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
            if self._motion is not None:
                self._motion.awake()
            self._resume_playback_after_wake()
            if not self._recording:
                self.robot.media.start_recording()
                self._recording = True
            self._set_status(
                "waiting_for_wake_word",
                _WAKE_PROMPT,
                power_mode=mode,
                meeting_seconds_remaining=0,
                last_error="",
            )

    def _set_motor_mode(self, enabled: bool, *, wake: bool = False) -> None:
        """Change motor torque through the daemon's supported local API or fail explicitly."""
        with self._motor_transition_lock:
            self._set_motor_mode_unlocked(enabled, wake=wake)

    def _resume_playback_after_wake(self) -> None:
        self._playback.awake()
        if self._playback_stopped_for_privacy:
            self._playback.start_playing()
            self._playback_stopped_for_privacy = False

    def _set_motor_mode_unlocked(self, enabled: bool, *, wake: bool = False) -> None:
        if enabled and self.stop_event.is_set() and (wake or not getattr(self, "_safety_fold_active", False)):
            raise RuntimeError("Runtime is stopping; motor enable is blocked")
        if self._native_sleep_uncertain:
            raise RuntimeError("Native sleep outcome is unknown; motor transitions are blocked")
        # A second caller can queue Awake using status from before the first wake.
        # Re-read hardware under the lock, rather than trusting that stale app status.
        if enabled and wake and self._motors_enabled is True and not self._head_safely_folded:
            if self._wake_pose_is_ready():
                if self._motion is not None:
                    self._motion.awake()
                self._resume_playback_after_wake()
                _LOGGER.debug("Reachy is already physically Awake; skipping duplicate wake motion")
                return
        mode = "enabled" if enabled else "disabled"
        try:
            response = httpx.post(f"http://127.0.0.1:8000/api/motors/set_mode/{mode}", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            if isinstance(exc, httpx.TransportError) or (
                isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500
            ):
                raise self._motor_backend_unavailable(exc) from exc
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
                    if self._motion is not None:
                        self._motion.awake()
                    self._resume_playback_after_wake()
                    _LOGGER.warning(
                        "Reachy wake call raised after the measured head reached Awake; preserving enabled torque: %s",
                        exc,
                    )
                    return
                message = f"Reachy motor torque was enabled, but the wake motion failed: {exc}"
                _LOGGER.exception(message)
                raise RuntimeError(message) from exc
            if self._motion is not None:
                self._motion.awake()
            self._resume_playback_after_wake()
        _LOGGER.info("Reachy motors %s%s", mode, " with wake motion" if wake else "")

    def _wake_pose_is_ready(self) -> bool:
        """Confirm the native wake motion reached a clearly unfolded pose."""
        try:
            response = httpx.get("http://127.0.0.1:8000/api/state/full", timeout=2.0)
            response.raise_for_status()
            state = response.json()
            control_mode = state["control_mode"]
            if control_mode not in {"enabled", "disabled", "gravity_compensation"}:
                raise ValueError("Motor control mode is unverified")
            pose = state["head_pose"]
            z = float(pose["z"])
            pitch = float(pose["pitch"])
            if not math.isfinite(z) or not math.isfinite(pitch):
                raise ValueError("Awake-pose measurement is not finite")
            return control_mode == "enabled" and z > -0.02 and pitch < 0.20
        except Exception as exc:
            raise self._motor_backend_unavailable(exc) from exc
