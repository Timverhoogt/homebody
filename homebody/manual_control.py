"""Owner robot controls: manual and precision actions, live pose, and camera-joystick sessions.

``ManualControlMixin`` is mixed into ``HermesVoiceRuntime`` and keeps its state on the runtime
instance. Every entry point takes ``_motor_transition_lock`` before ``_kids_lock`` and then
``_camera_control_lock``, matching the runtime's lock order.
"""

from __future__ import annotations

import logging
import math
import secrets
import threading
import time

import httpx

from .robot_tools import CameraJoystickStream, manual_precision_action, manual_robot_action
from .safety_gate import CAMERA_CONTROL_POLICY

# Keep the runtime logger name so existing log filters still match control lines.
_LOGGER = logging.getLogger("homebody.runtime")

# A joystick gesture that sends nothing for this long is abandoned (closed tab, lost network).
_CAMERA_CONTROL_IDLE_TIMEOUT_SECONDS = 30.0
# A green-button press that the wake loop cannot take within this window is dropped.
_BUTTON_WAKE_WINDOW_SECONDS = 2.0


class ManualControlMixin:
    """Allow-listed owner movement and short-lived camera-feed joystick sessions."""

    def _init_manual_control_state(self) -> None:
        self._camera_control_lock = threading.RLock()
        self._camera_control_session_id = ""
        self._camera_control_last_sequence = 0
        self._camera_control_last_activity = 0.0
        self._camera_control_stream: CameraJoystickStream | None = None

    def queue_manual_robot_action(
        self,
        action: str,
        value: str,
        *,
        wake_if_standby: bool = True,
    ) -> dict[str, object]:
        """Queue one allow-listed UI action only after a serialized, confirmed wake."""
        name, arguments = manual_robot_action(action, value)
        with self._motor_transition_lock:
            with self._kids_lock:
                if self._kids_active:
                    raise RuntimeError("Manual robot controls are blocked while Kids Mode is active")
                mode = self._effective_power_mode()
                if mode in {"meeting", "sleep"} or self._privacy_requested.is_set():
                    raise RuntimeError("Manual robot control is blocked in Meeting and Sleep")
                if self._actions is None:
                    raise RuntimeError("Robot action controller is not ready")
                if mode == "standby":
                    if not wake_if_standby:
                        raise RuntimeError("Gesture reactions never wake Reachy automatically")
                    self.set_power_mode("awake")
                if self._effective_power_mode() != "awake" or self._motors_enabled is not True:
                    raise RuntimeError("Manual robot control requires confirmed Awake motor torque")
                with self._camera_control_lock:
                    if self._camera_control_session_live_locked():
                        raise RuntimeError("Manual robot controls are blocked during camera control")
                    result = self._actions.enqueue(
                        name,
                        arguments,
                        hold_pose=action.strip().lower() == "look",
                        reject_if_busy=True,
                    )
            if not result.get("accepted"):
                raise RuntimeError(str(result.get("error") or "Robot action could not be queued"))
            _LOGGER.info("Manual robot control queued: %s %s", action, value)
            return {
                "ok": True,
                "action": action.strip().lower(),
                "value": value.strip().lower(),
                "power_mode": self._effective_power_mode(),
                **result,
            }

    def robot_pose(self) -> dict[str, float]:
        """Read a sanitized Cartesian pose from Reachy's local daemon."""
        try:
            response = httpx.get("http://127.0.0.1:8000/api/state/full", timeout=2.0)
            response.raise_for_status()
            state = response.json()
            if not isinstance(state, dict):
                raise ValueError("daemon state is not an object")
            head = state.get("head_pose")
            required = ("x", "y", "z", "roll", "pitch", "yaw")
            if not isinstance(head, dict) or any(key not in head for key in required):
                raise ValueError("daemon head pose is incomplete")
            if "body_yaw" not in state:
                raise ValueError("daemon body yaw is missing")
            values: dict[str, float] = {}
            for key in required:
                raw = head[key]
                if isinstance(raw, bool):
                    raise ValueError(f"daemon pose field {key} is not numeric")
                values[key] = float(raw)
            raw_body = state["body_yaw"]
            if isinstance(raw_body, bool):
                raise ValueError("daemon body yaw is not numeric")
            values["body_yaw"] = float(raw_body)
            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError("daemon pose contains a non-finite value")
            return {
                "x": round(values["x"] * 1000.0, 2),
                "y": round(values["y"] * 1000.0, 2),
                "z": round(values["z"] * 1000.0, 2),
                "roll": round(math.degrees(values["roll"]), 2),
                "pitch": round(math.degrees(values["pitch"]), 2),
                "yaw": round(math.degrees(values["yaw"]), 2),
                "body_yaw": round(math.degrees(values["body_yaw"]), 2),
            }
        except Exception as exc:
            raise RuntimeError(f"Could not read Reachy pose: {exc}") from exc

    def _assert_camera_control_policy(self) -> None:
        self._safety_gate.require(CAMERA_CONTROL_POLICY)

    def _camera_control_session_live_locked(self) -> bool:
        """Return whether a joystick session is live, expiring one abandoned past the idle timeout.

        Every reader of camera-control ownership goes through this, so a closed tab cannot keep
        presence, gestures, presentation, and manual controls blocked. Hold _camera_control_lock.
        """
        if not self._camera_control_session_id:
            return False
        if time.monotonic() - self._camera_control_last_activity <= _CAMERA_CONTROL_IDLE_TIMEOUT_SECONDS:
            return True
        _LOGGER.info("Expiring an abandoned camera-control session")
        stream = self._camera_control_stream
        self._camera_control_stream = None
        self._camera_control_session_id = ""
        self._camera_control_last_sequence = 0
        self._camera_control_last_activity = 0.0
        if stream is not None:
            stream.stop()
        return False

    def _validate_camera_control_session(self, session_id: str, sequence: int) -> None:
        if not self._camera_control_session_id or session_id != self._camera_control_session_id:
            raise RuntimeError("Camera control session is not active")
        if not self._camera_control_session_live_locked():
            raise RuntimeError("Camera control session expired")
        if sequence <= self._camera_control_last_sequence:
            raise RuntimeError("Camera control sequence is stale or replayed")
        self._camera_control_last_sequence = sequence
        self._camera_control_last_activity = time.monotonic()

    def start_camera_control(
        self,
        *,
        camera_feed_enabled: bool,
        controls_enabled: bool,
        adult_ui_unlocked: bool,
    ) -> dict[str, object]:
        """Create a short-lived, owner-initiated movement session for one pointer gesture."""
        if not adult_ui_unlocked:
            raise RuntimeError("Camera controls require an unlocked adult UI")
        if not camera_feed_enabled or not controls_enabled:
            raise RuntimeError("Camera feed and camera movement controls must both be enabled")
        with self._motor_transition_lock:
            with self._kids_lock:
                self._assert_camera_control_policy()
                assert self._actions is not None
                if self._actions.busy or self._actions.pending_count:
                    raise RuntimeError("Robot is busy")
                with self._camera_control_lock:
                    pose = self.robot_pose()
                    stream = CameraJoystickStream()
                    result = self._actions.enqueue(
                        "camera_joystick",
                        {"stream": stream, "body_yaw_degrees": pose["body_yaw"]},
                        hold_pose=True,
                        reject_if_busy=True,
                    )
                    if not result.get("accepted"):
                        raise RuntimeError(str(result.get("error") or "Camera movement could not start"))
                    self._camera_control_session_id = f"camera-{secrets.token_hex(16)}"
                    self._camera_control_last_sequence = 0
                    self._camera_control_last_activity = time.monotonic()
                    self._camera_control_stream = stream
                    return {"ok": True, "session_id": self._camera_control_session_id}

    def queue_camera_control(
        self,
        session_id: str,
        sequence: int,
        pan: float,
        tilt: float,
    ) -> dict[str, object]:
        """Queue one bounded joystick increment for the active pointer gesture."""
        if any(isinstance(value, bool) for value in (pan, tilt)) or not all(
            math.isfinite(value) and -1.0 <= value <= 1.0 for value in (pan, tilt)
        ):
            raise RuntimeError("Camera control input must be finite and between -1 and 1")
        with self._motor_transition_lock:
            with self._kids_lock:
                self._assert_camera_control_policy()
                with self._camera_control_lock:
                    self._validate_camera_control_session(session_id, sequence)
                    stream = self._camera_control_stream
                    if stream is None:
                        raise RuntimeError("Camera joystick stream is not active")
                    stream.update(float(pan), float(tilt))
                return {"ok": True, "sequence": sequence, "accepted": True, "streamed": True}

    def center_camera_control(self, session_id: str, sequence: int) -> dict[str, object]:
        """Explicitly return the camera head and body yaw to neutral."""
        with self._motor_transition_lock:
            with self._kids_lock:
                self._assert_camera_control_policy()
                with self._camera_control_lock:
                    self._validate_camera_control_session(session_id, sequence)
                    assert self._actions is not None
                    stream = self._camera_control_stream
                    self._camera_control_stream = None
                    pose = self.robot_pose()
                    if stream is not None:
                        stream.stop(hold_body_yaw_degrees=pose["body_yaw"])
                    self._actions.cancel(stop_media=False)
                    if not self._actions.wait_idle(timeout=1.0):
                        raise RuntimeError("Camera movement did not stop before Center")
                    result = self._actions.enqueue(
                        "nudge_reachy",
                        {"axis": "center_all", "delta": 0.0, "body_yaw_degrees": pose["body_yaw"]},
                        hold_pose=True,
                        reject_if_busy=True,
                    )
                if not result.get("accepted"):
                    raise RuntimeError(str(result.get("error") or "Camera center movement could not be queued"))
                if not self._actions.wait_idle(timeout=5.0):
                    raise RuntimeError("Camera center movement did not complete in time")
                return {"ok": True, "sequence": sequence, **result}

    def end_camera_control(self, session_id: str, sequence: int) -> dict[str, object]:
        """Invalidate one pointer gesture and freeze the current measured viewpoint."""
        with self._camera_control_lock:
            self._validate_camera_control_session(session_id, sequence)
            self._camera_control_session_id = ""
            stream = self._camera_control_stream
            self._camera_control_stream = None
        if self._actions is None:
            raise RuntimeError("Robot action controller is not ready")
        if stream is not None:
            try:
                stream.stop(hold_body_yaw_degrees=self.robot_pose()["body_yaw"])
            except RuntimeError:
                stream.stop()
        self._actions.cancel(stop_media=False)
        held = self._actions.wait_idle(timeout=1.0)
        if not held:
            raise RuntimeError("Camera movement did not stop in time")
        return {"ok": True, "held": True}

    def invalidate_camera_control(self) -> None:
        """Reject delayed gesture packets after Stop, privacy, power, or session changes."""
        with self._camera_control_lock:
            stream = self._camera_control_stream
            self._camera_control_stream = None
            self._camera_control_session_id = ""
            self._camera_control_last_sequence = 0
            self._camera_control_last_activity = 0.0
        if stream is not None:
            stream.stop()

    def revoke_camera_control(self) -> None:
        """Invalidate an active gesture and cooperatively freeze its movement."""
        with self._camera_control_lock:
            was_active = bool(self._camera_control_session_id)
            stream = self._camera_control_stream
            self._camera_control_stream = None
            self._camera_control_session_id = ""
            self._camera_control_last_sequence = 0
            self._camera_control_last_activity = 0.0
        if stream is not None:
            stream.stop()
        if was_active and self._actions is not None:
            self._actions.cancel(stop_media=False)
            self._actions.wait_idle(timeout=1.0)

    def queue_precision_robot_action(self, axis: str, delta: float) -> dict[str, object]:
        """Queue one bounded Cartesian nudge after confirmed motor wake."""
        with self._motor_transition_lock:
            with self._kids_lock:
                if self._kids_active:
                    raise RuntimeError("Precision robot controls are blocked while Kids Mode is active")
                mode = self._effective_power_mode()
                if mode in {"meeting", "sleep"} or self._privacy_requested.is_set():
                    raise RuntimeError("Precision robot control is blocked in Meeting and Sleep")
                if self._actions is None:
                    raise RuntimeError("Robot action controller is not ready")
                if mode == "standby":
                    self.set_power_mode("awake")
                if self._effective_power_mode() != "awake" or self._motors_enabled is not True:
                    raise RuntimeError("Precision robot control requires confirmed Awake motor torque")
                with self._camera_control_lock:
                    if self._camera_control_session_live_locked():
                        raise RuntimeError("Precision robot controls are blocked during camera control")
                    pose = self.robot_pose()
                    name, arguments = manual_precision_action(
                        axis,
                        delta,
                        body_yaw_degrees=pose["body_yaw"],
                    )
                    result = self._actions.enqueue(
                        name,
                        arguments,
                        hold_pose=True,
                        reject_if_busy=True,
                    )
            if not result.get("accepted"):
                raise RuntimeError(str(result.get("error") or "Precision movement could not be queued"))
            _LOGGER.info("Precision robot movement queued: %s %s", axis, delta)
            return {
                "ok": True,
                "axis": axis.strip().lower(),
                "delta": float(delta),
                "power_mode": self._effective_power_mode(),
                **result,
            }

    def stop_manual_robot_action(self) -> dict[str, object]:
        """Cancel physical movement without changing power mode or starting new motion."""
        # Every physical Stop entry point (PWA, gamepad, or future local control)
        # must invalidate agent work before checking motion-controller readiness.
        self.cancel_agent_work("emergency_stop")
        self.invalidate_camera_control()
        if self._actions is None:
            raise RuntimeError("Robot action controller is not ready")
        pending_before = self._actions.pending_count
        active_cancelled = self._actions.cancel(stop_media=False)
        queued_cancelled = max(0, pending_before - int(active_cancelled))
        robot_stopped = self._actions.wait_idle(timeout=5.0)
        if self._motion is not None:
            self._motion.resume()
        _LOGGER.info(
            "Manual robot control stopped; active_cancelled=%s queued_cancelled=%s",
            active_cancelled,
            queued_cancelled,
        )
        if not robot_stopped:
            raise RuntimeError("Robot action controller did not become idle after Stop")
        return {
            "ok": True,
            "robot_stopped": robot_stopped,
            "active_cancelled": active_cancelled,
            "queued_cancelled": queued_cancelled,
        }

    def physical_stop(self) -> dict[str, object]:
        """Red-button Stop: end voice, Kids Mode, announcements, offers, and movement together.

        Every step runs even when an earlier one fails, so one unready subsystem never leaves
        another one moving or talking. Failures are reported together afterwards.
        """
        errors: list[str] = []
        steps: list[tuple[str, object]] = [
            ("agent", lambda: self.cancel_agent_work("emergency_stop")),
            ("offer", lambda: self.cancel_contextual_offer("emergency_stop")),
            ("presentation", lambda: self.stop_presentation_window("emergency_stop")),
            ("announcements", lambda: self.stop_announcements(clear_queue=True)),
        ]
        with self._kids_lock:
            kids_active = self._kids_active or self._kids_locked
        if kids_active:
            steps.append(("kids", lambda: self.stop_kids_mode(reason="physical_stop", fold=True)))
        steps.append(("robot", self.stop_manual_robot_action))
        for name, step in steps:
            try:
                step()  # type: ignore[operator]
            except Exception as exc:
                _LOGGER.warning("Physical Stop could not stop %s: %s", name, exc)
                errors.append(f"{name}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors)[:300])
        return {"ok": True, "kids_stopped": kids_active}

    def request_button_wake(self) -> str:
        """Green-button press: wake from Sleep/Meeting, otherwise start a turn as the wake word does."""
        mode = self._effective_power_mode()
        if mode in {"meeting", "sleep"}:
            self.set_power_mode("awake")
            return "awake"
        # The wake loop consumes this on its next audio frame; a stale press simply expires.
        self._button_wake_deadline = time.monotonic() + _BUTTON_WAKE_WINDOW_SECONDS
        return "listen"

    def _take_button_wake(self) -> bool:
        deadline, self._button_wake_deadline = self._button_wake_deadline, 0.0
        return 0.0 < deadline and time.monotonic() <= deadline
