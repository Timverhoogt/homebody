"""Camera capture, local gesture reactions, face tracking, and voice direction-of-arrival for the runtime.

``VisionMixin`` is mixed into ``HermesVoiceRuntime`` and keeps its state on the runtime instance.
Camera access is gated by ``safety_gate`` and, for I Spy, by the consent-bound Kids capture check.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from .config import AppConfig
from .gesture_detection import GestureDetector, GestureReactionGate
from .presence import PresenceObservation
from .safety_gate import CAMERA_CAPTURE_POLICY, GESTURE_POLICY

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("reachy_mini_hermes.runtime")

def doa_yaw_degrees(angle_radians: float) -> float:
    """Convert XVF3800 DOA coordinates to a conservative Reachy head yaw."""
    yaw = -(math.degrees(angle_radians) - 90.0) * 0.8
    if abs(yaw) < 10.0:
        return 0.0
    return round(max(-60.0, min(60.0, yaw)), 1)


class VisionMixin:
    """One-frame camera capture, the gesture worker, face tracking, and direction-of-arrival orientation."""

    def _init_vision_state(self) -> None:
        self._camera_lock = threading.Lock()
        self._face_tracking_active = False
        self._face_tracking_desired = False
        self._face_tracking_weight = 0.65
        self._last_doa_sample_at = 0.0
        self._last_valid_doa_at = 0.0
        self._last_valid_doa_angle: float | None = None
        self._gesture_worker: threading.Thread | None = None
        self._gesture_stop_requested = threading.Event()

    def test_camera(self) -> dict[str, object]:
        """Capture one frame locally for setup diagnostics without returning it."""
        jpeg = self.camera_snapshot()
        return {"bytes": len(jpeg), "content_type": "image/jpeg"}

    def _assert_camera_allowed(self) -> None:
        self._safety_gate.require(CAMERA_CAPTURE_POLICY)

    def camera_snapshot(self) -> bytes:
        """Capture one bounded JPEG for an explicitly authenticated request."""
        self._assert_camera_allowed()
        jpeg = self._capture_camera_jpeg()
        with self._status_lock:
            self._status.camera_captures += 1
            self._status.camera_last_error = ""
        return jpeg

    def _capture_camera_jpeg(self, *, kids_generation: int | None = None) -> bytes:
        media = getattr(self.robot, "media", None)
        capture = getattr(media, "get_frame_jpeg", None)
        if not callable(capture):
            raise RuntimeError("Reachy camera capture is unavailable")

        def assert_allowed() -> None:
            if kids_generation is None:
                self._assert_camera_allowed()
                return
            with self._kids_lock:
                if (
                    not self._kids_active
                    or self._kids_generation != kids_generation
                    or not self._kids_camera_active
                    or self._kids_profile is None
                    or self._kids_profile.activity != "ispy"
                    or not self._kids_profile.camera_consent
                ):
                    raise RuntimeError("I Spy camera capture was cancelled")

        with self._camera_lock:
            for _ in range(60):
                assert_allowed()
                frame = capture()
                if frame:
                    assert_allowed()
                    if not isinstance(frame, (bytes, bytearray, memoryview)):
                        raise RuntimeError("Reachy camera returned an unsupported JPEG payload")
                    jpeg = bytes(frame)
                    maximum = 1_500_000 if kids_generation is not None else 1_000_000
                    if len(jpeg) > maximum:
                        raise RuntimeError("Camera JPEG exceeded the active safety limit")
                    return jpeg
                time.sleep(0.05)
        raise RuntimeError("Reachy camera did not return a frame")

    def _gesture_detection_allowed(self, config: AppConfig) -> bool:
        if not config.gesture_detection_enabled or not config.home_assistant_controls_enabled:
            return False
        return self._safety_gate.allows(GESTURE_POLICY)

    def _clear_gesture_detection_state(self) -> None:
        with self._status_lock:
            self._status.gesture_detection_active = False
            self._status.gesture_detected = "none"
            self._status.gesture_confidence = 0.0

    def _process_gesture_once(
        self,
        detector: GestureDetector,
        gate: GestureReactionGate,
        *,
        now: float,
    ) -> bool:
        config = self.config_loader()
        if not self._gesture_detection_allowed(config):
            gate.reset()
            self._clear_gesture_detection_state()
            return False
        try:
            jpeg = self._capture_camera_jpeg()
            gesture, confidence = detector.detect_jpeg(jpeg)
            with self._status_lock:
                self._status.gesture_detection_active = True
                self._status.gesture_detected = "none" if gesture == "no_gesture" else gesture
                self._status.gesture_confidence = round(float(confidence), 4)
                self._status.gesture_frames_processed += 1
                self._status.gesture_last_error = ""
            reaction = gate.update(gesture, confidence, now=now)
            if reaction is not None:
                action, value = reaction
                self.observe_presence(
                    PresenceObservation(
                        source="gesture",
                        occupied=True,
                        attentive=True,
                        confidence=float(confidence),
                    ),
                    allow_acknowledgement=False,
                )
                self.queue_manual_robot_action(action, value, wake_if_standby=False)
                with self._status_lock:
                    self._status.gesture_reactions += 1
                    self._status.gesture_last_action = f"{gesture}:{action}:{value}"
                _LOGGER.info("Gesture reaction queued: %s -> %s %s", gesture, action, value)
            return True
        except Exception as exc:
            with self._status_lock:
                self._status.gesture_detection_active = False
                self._status.gesture_last_error = str(exc)
            _LOGGER.warning("Gesture detection iteration failed: %s", exc)
            return False

    def _run_gesture_worker(self) -> None:
        detector: GestureDetector | None = None
        gate = GestureReactionGate(required_frames=3, clear_frames=2, cooldown_seconds=8.0, min_confidence=0.70)
        next_model_attempt = 0.0
        try:
            while not self.stop_event.is_set() and not self._gesture_stop_requested.is_set():
                started_at = time.monotonic()
                config = self.config_loader()
                if not config.gesture_detection_enabled:
                    if detector is not None:
                        detector.close()
                        detector = None
                    gate.reset()
                    self._clear_gesture_detection_state()
                    self._gesture_stop_requested.wait(0.25)
                    continue
                if not self._gesture_detection_allowed(config):
                    gate.reset()
                    self._clear_gesture_detection_state()
                    self._gesture_stop_requested.wait(0.25)
                    continue
                if detector is None:
                    if started_at < next_model_attempt:
                        self._gesture_stop_requested.wait(min(0.25, next_model_attempt - started_at))
                        continue
                    try:
                        detector = GestureDetector(self.assets / "gesture_models")
                        with self._status_lock:
                            self._status.gesture_last_error = ""
                        _LOGGER.info("On-device gesture detector loaded")
                    except Exception as exc:
                        next_model_attempt = started_at + 10.0
                        with self._status_lock:
                            self._status.gesture_detection_active = False
                            self._status.gesture_last_error = str(exc)
                        _LOGGER.warning("Could not load gesture detector: %s", exc)
                        self._gesture_stop_requested.wait(0.5)
                        continue
                self._process_gesture_once(detector, gate, now=started_at)
                elapsed = time.monotonic() - started_at
                self._gesture_stop_requested.wait(max(0.0, (1.0 / 3.0) - elapsed))
        finally:
            if detector is not None:
                detector.close()
            self._clear_gesture_detection_state()

    def _set_face_tracking(self, enabled: bool, *, weight: float | None = None) -> None:
        """Control daemon-local face tracking and mirror the real state in status."""
        if enabled == self._face_tracking_active and (weight is None or weight == self._face_tracking_weight):
            return
        try:
            if enabled:
                self._face_tracking_weight = float(weight if weight is not None else self._face_tracking_weight)
                self.robot.start_head_tracking(weight=self._face_tracking_weight)
            else:
                if self._face_tracking_active:
                    self.robot.stop_head_tracking()
            self._face_tracking_active = enabled
            with self._status_lock:
                self._status.face_tracking_active = enabled
            _LOGGER.info("Local face tracking %s", "enabled" if enabled else "disabled")
        except Exception as exc:
            self._face_tracking_active = False
            with self._status_lock:
                self._status.face_tracking_active = False
            _LOGGER.warning("Could not change local face tracking: %s", exc)

    def _sample_doa(self, *, force: bool = False) -> float | None:
        """Cache a recent valid local microphone-array direction estimate."""
        now = time.monotonic()
        if not force and now - self._last_doa_sample_at < 0.1:
            return self._last_valid_doa_angle
        self._last_doa_sample_at = now
        getter = getattr(self.robot.media, "get_DoA", None)
        if not callable(getter):
            return None
        result = getter()
        if not isinstance(result, tuple) or len(result) < 2 or not bool(result[1]):
            return None
        angle_radians = float(result[0])
        if not math.isfinite(angle_radians):
            return None
        self._last_valid_doa_angle = angle_radians
        self._last_valid_doa_at = now
        return angle_radians

    def _orient_to_voice(self, config: AppConfig) -> None:
        """Turn once toward a recent local wake-phrase direction estimate."""
        if not config.doa_enabled or self._motion is None:
            return
        try:
            angle_radians = self._sample_doa(force=True)
            if angle_radians is None:
                age = time.monotonic() - self._last_valid_doa_at
                if self._last_valid_doa_angle is None or age > 1.5:
                    _LOGGER.debug("No recent speech-validated DOA is available for wake orientation")
                    return
                angle_radians = self._last_valid_doa_angle
            yaw = doa_yaw_degrees(angle_radians)
            with self._status_lock:
                self._status.doa_angle_degrees = round(math.degrees(angle_radians), 1)
            if yaw:
                self._motion.orient_to_sound(yaw)
            _LOGGER.info(
                "Local wake DOA: %.1f degrees, Reachy yaw %.1f degrees",
                math.degrees(angle_radians),
                yaw,
            )
        except Exception as exc:
            _LOGGER.warning("Could not orient to local wake DOA: %s", exc)
