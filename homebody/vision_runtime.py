"""Camera capture, local gesture reactions, face tracking, and voice direction-of-arrival for the runtime.

``VisionMixin`` is mixed into ``HermesVoiceRuntime`` and keeps its state on the runtime instance.
Camera access is gated by ``safety_gate`` and, for I Spy, by the consent-bound Kids capture check.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from . import face_model
from .config import AppConfig
from .gesture_detection import GestureDetector, GestureReactionGate
from .ispy import ISpyCancelled
from .local_vision import LocalVisionClient, LocalVisionError
from .platform_info import onnx_cache_dir, onnx_session_providers
from .presence import PresenceObservation
from .safety_gate import CAMERA_CAPTURE_POLICY, GESTURE_POLICY

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")


def doa_yaw_degrees(angle_radians: float) -> float:
    """Convert XVF3800 DOA coordinates to a conservative Reachy head yaw."""
    yaw = -(math.degrees(angle_radians) - 90.0) * 0.8
    if abs(yaw) < 10.0:
        return 0.0
    return round(max(-60.0, min(60.0, yaw)), 1)


class _FaceFollowLease:
    """Identity of the one voice turn allowed to follow a face (no face data, ids or frames)."""

    __slots__ = ("kids_generation", "kids_session_id", "stop_generation", "wanted", "weight")

    def __init__(
        self, stop_generation: int, kids_session_id: str, kids_generation: int, weight: float, wanted: bool,
    ) -> None:
        self.stop_generation = stop_generation
        self.kids_session_id = kids_session_id
        self.kids_generation = kids_generation
        self.weight = weight
        self.wanted = wanted


class VisionMixin:
    """One-frame camera capture, the gesture worker, face tracking, and direction-of-arrival orientation."""

    def _init_vision_state(self) -> None:
        self._camera_lock = threading.Lock()
        self._camera_recovery_specs = None
        self._face_tracking_active = False
        self._face_tracking_desired = False  # mirrors "the current lease wants following" (diagnostics)
        self._face_tracking_weight = 0.65
        # One lease per admitted voice turn owns local following. The lock is a leaf: nothing is
        # acquired while it is held except the tracker command and the status lock, so stop paths
        # may take it while holding _motor_transition_lock or _kids_lock.
        self._face_follow_lock = threading.Lock()
        self._face_follow_lease: _FaceFollowLease | None = None
        self._face_follow_yields: set[str] = set()
        self._face_follow_epoch = 0
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

    def _new_local_vision_client(self, config: AppConfig) -> LocalVisionClient:
        return LocalVisionClient(config.local_vision_url, config.local_vision_model)

    def describe_camera_view(self, question: str, *, config: AppConfig | None = None) -> dict[str, object]:
        """Answer a question about one fresh frame with the local vision model; the frame stays local."""
        config = config or self.config_loader()
        if not config.local_vision_enabled:
            raise RuntimeError("Local vision is turned off in Reachy settings")
        if not config.camera_enabled:
            raise RuntimeError("Camera access is disabled in Reachy settings")
        jpeg = self.camera_snapshot()
        try:
            with self._new_local_vision_client(config) as client:
                result = client.describe(jpeg, question)
        except LocalVisionError as exc:
            with self._status_lock:
                self._status.local_vision_last_error = str(exc)
            raise RuntimeError(str(exc)) from exc
        finally:
            del jpeg
        with self._status_lock:
            self._status.local_vision_answers += 1
            self._status.local_vision_last_latency_ms = int(result["latency_ms"])  # type: ignore[call-overload]
            self._status.local_vision_last_error = ""
        _LOGGER.info("Local vision answered in %s ms with %s", result["latency_ms"], result["model"])
        return result

    def _capture_camera_jpeg(
        self,
        *,
        kids_generation: int | None = None,
        turn_bound: bool = False,
        is_current: Callable[[], bool] | None = None,
        adult_current: Callable[[], None] | None = None,
        show_tell: Callable[[], None] | None = None,
    ) -> bytes:
        media = getattr(self.robot, "media", None)
        capture = getattr(media, "get_frame_jpeg", None)
        if not callable(capture):
            raise RuntimeError("Reachy camera capture is unavailable")
        if show_tell is not None and kids_generation is None:
            raise RuntimeError("Show and Tell capture needs its Kids generation")

        def assert_allowed() -> None:
            if self.stop_event.is_set():
                cancelled = RuntimeError if kids_generation is None else ISpyCancelled
                raise cancelled("Camera capture was cancelled")
            if is_current is not None and not is_current():
                raise RuntimeError("Camera capture was cancelled")
            if show_tell is not None:
                # Kids Chat Show and Tell: its own consent permit, never the I Spy consent.
                with self._kids_lock:
                    if (
                        not self._kids_active
                        or self._kids_generation != kids_generation
                        or not self._kids_camera_active
                        or self._kids_profile is None
                        or self._kids_profile.activity != "buddy"
                        or self._kids_profile.host
                    ):
                        raise ISpyCancelled("Show and Tell camera capture was cancelled")
                if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                    raise ISpyCancelled("Show and Tell camera capture was cancelled by privacy")
                show_tell()
                return
            if kids_generation is None:
                self._assert_camera_allowed()
                if adult_current is not None:
                    adult_current()
                return
            if turn_bound and self._turn_stop_requested():
                raise ISpyCancelled("I Spy camera capture was stopped")
            with self._kids_lock:
                if (
                    not self._kids_active
                    or self._kids_generation != kids_generation
                    or not self._kids_camera_active
                    or self._kids_profile is None
                    or self._kids_profile.activity != "ispy"
                    or not self._kids_profile.camera_consent
                ):
                    raise ISpyCancelled("I Spy camera capture was cancelled")
            if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                raise ISpyCancelled("I Spy camera capture was cancelled by privacy")

        with self._camera_lock:
            for attempt in range(60):
                assert_allowed()
                if attempt == 10:
                    self._reconnect_local_camera_reader(assert_allowed)
                    assert_allowed()
                frame = capture()
                if frame:
                    assert_allowed()
                    if not isinstance(frame, (bytes, bytearray, memoryview)):
                        raise RuntimeError("Reachy camera returned an unsupported JPEG payload")
                    jpeg = bytes(frame)
                    maximum = 1_500_000 if kids_generation is not None and show_tell is None else 1_000_000
                    if len(jpeg) > maximum:
                        raise RuntimeError("Camera JPEG exceeded the active safety limit")
                    return jpeg
                time.sleep(0.05)
        raise RuntimeError("Reachy camera did not return a frame")

    def _reconnect_local_camera_reader(self, assert_allowed: Callable[[], None]) -> bool:
        """Rebuild only the app's stale IPC reader, never daemon media, audio, or motors.

        Called under _camera_lock, once per bounded capture. SDK 1.9 close/open reuse
        leaves a quit GLib loop, so use its verified local-camera initializer instead.
        """
        media = self.robot.media
        backend = getattr(media, "backend", None)
        if getattr(backend, "value", backend) != "local":
            return False
        initialize = getattr(media, "_init_camera", None)
        camera = getattr(media, "camera", None)
        if not callable(initialize):
            return False
        if camera is not None:
            if not callable(getattr(camera, "close", None)):
                return False
            self._camera_recovery_specs = getattr(camera, "camera_specs", None)
        elif self._camera_recovery_specs is None:
            return False
        assert_allowed()
        try:
            if camera is not None:
                camera.close()
                media.camera = None
            assert_allowed()
            initialize(log_level="INFO", camera_specs=self._camera_recovery_specs)
            assert_allowed()
        except Exception:
            replacement = getattr(media, "camera", None)
            if replacement is not None and replacement is not camera:
                try:
                    replacement.close()
                finally:
                    media.camera = None
            raise
        _LOGGER.info("Reconnected Homebody's local camera reader after empty frames")
        return True

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
                        detector = GestureDetector(
                            self.assets / "gesture_models",
                            providers=onnx_session_providers(config.local_ai_accelerator, cache_dir=onnx_cache_dir()),
                        )
                        with self._status_lock:
                            self._status.gesture_last_error = ""
                            accelerator = detector.active_provider.replace("ExecutionProvider", "")
                            self._status.gesture_accelerator = accelerator
                        _LOGGER.info("On-device gesture detector loaded on %s", detector.active_provider)
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

    # ---- Local face following ownership --------------------------------------------------------
    # The daemon-local tracker is the only producer; Homebody never sees, identifies or keeps a face.
    # A lease binds it to one admitted turn (Everyday or Kids). Any stop path bumps the epoch and
    # turns it off under _face_follow_lock, so a sync that sampled authority earlier can never
    # restart it. Exclusive motion/camera owners yield it; only a still-current lease resumes it.

    def _begin_face_follow(
        self, config: AppConfig, stop_generation: int, *, kids_generation: int | None = None,
    ) -> _FaceFollowLease:
        """Bind following to this turn. Kids configs already fold in the profile's motion switch."""
        kids_session_id, wanted = "", bool(config.face_tracking_enabled)
        if config.kids_mode_enabled:
            kids_session_id = config.kids_session_id
            with self._kids_lock:
                current = self._kids_active and bool(kids_session_id) and self._kids_session_id == kids_session_id
                if kids_generation is None:
                    kids_generation = self._kids_generation if current else -1
            wanted = wanted and current
        lease = _FaceFollowLease(
            stop_generation, kids_session_id, -1 if kids_generation is None else kids_generation,
            float(config.face_tracking_weight), wanted,
        )
        with self._face_follow_lock:
            self._face_follow_epoch += 1
            self._face_follow_lease = lease
            self._face_follow_yields.clear()
            self._face_tracking_desired = wanted
        self._sync_face_follow()
        return lease

    def _end_face_follow(self, lease: _FaceFollowLease | None = None) -> None:
        """The owning turn ended: stop now. A given lease only ends itself, never a newer turn's."""
        with self._face_follow_lock:
            if lease is not None and self._face_follow_lease is not lease:
                return
            self._face_follow_epoch += 1
            self._face_follow_lease = None
            self._face_follow_yields.clear()
            self._face_tracking_desired = False
            self._set_face_tracking(False)

    def _stop_face_follow(self) -> None:
        """Stop following immediately; the lease alone decides whether a later sync may resume."""
        lock = getattr(self, "_face_follow_lock", None)
        if lock is None:
            return
        with lock:
            self._face_follow_epoch += 1
            self._set_face_tracking(False)

    def _yield_face_follow(self, reason: str) -> None:
        """An exclusive motion or camera owner takes the head; following stays off until it resumes."""
        with self._face_follow_lock:
            self._face_follow_epoch += 1
            self._face_follow_yields.add(reason)
            self._set_face_tracking(False)

    def _resume_face_follow(self, reason: str) -> None:
        with self._face_follow_lock:
            if reason not in self._face_follow_yields:
                return
            self._face_follow_yields.discard(reason)
        self._sync_face_follow()

    @contextmanager
    def _face_follow_yielded(self, reason: str) -> Iterator[None]:
        self._yield_face_follow(reason)
        try:
            yield
        finally:
            self._resume_face_follow(reason)

    def _set_face_follow_wanted(self, wanted: bool, *, weight: float | None = None) -> None:
        """Saved-setting change mid-turn (Home Assistant switch): applies to the current lease only."""
        with self._face_follow_lock:
            lease = self._face_follow_lease
            if lease is not None:
                if lease.kids_session_id:
                    wanted = wanted and lease.wanted  # a Kids turn never gains following it lacked
                lease.wanted = wanted
                if weight is not None:
                    lease.weight = float(weight)
                self._face_tracking_desired = wanted
            if not wanted or lease is None:
                self._face_follow_epoch += 1
                self._set_face_tracking(False)
                return
        self._sync_face_follow()

    def _face_follow_eligible(self, lease: _FaceFollowLease | None) -> bool:
        """Every authority is read under its own lock, never under _face_follow_lock."""
        if lease is None or not lease.wanted:
            return False
        if (
            self.stop_event.is_set() or self._privacy_requested.is_set()
            or self._effective_power_mode() in {"meeting", "sleep"}
            or self._motors_enabled is not True or self._head_safely_folded
            or getattr(self, "_safety_fold_active", False) or getattr(self, "_native_sleep_uncertain", False)
        ):
            return False
        with self._conversation_stop_generation_lock:
            if self._conversation_stop_generation != lease.stop_generation:
                return False
        if lease.kids_session_id:
            with self._kids_lock:
                profile = self._kids_profile
                if not (
                    self._kids_active and profile is not None and profile.motion_enabled
                    and self._kids_session_id == lease.kids_session_id
                    and self._kids_generation == lease.kids_generation
                    and time.monotonic() < self._kids_ends_at
                    and not self._kids_camera_active
                ):
                    return False
        camera_lock = getattr(self, "_camera_control_lock", None)
        if camera_lock is not None:
            with camera_lock:
                if self._camera_control_session_live_locked():
                    return False
        return True

    def _sync_face_follow(self) -> None:
        """Apply the current lease; a stop, yield or newer turn that ran meanwhile always wins."""
        with self._face_follow_lock:
            epoch, lease, yielded = self._face_follow_epoch, self._face_follow_lease, bool(self._face_follow_yields)
        enabled = not yielded and self._face_follow_eligible(lease)
        with self._face_follow_lock:
            if epoch != self._face_follow_epoch or lease is not self._face_follow_lease:
                return
            self._set_face_tracking(enabled, weight=lease.weight if enabled and lease is not None else None)

    def _set_face_tracking(self, enabled: bool, *, weight: float | None = None) -> None:
        """Control daemon-local face tracking and mirror the real state in status."""
        if enabled == self._face_tracking_active and (weight is None or weight == self._face_tracking_weight):
            return
        cache = face_model.check() if enabled else None  # offline hash; no camera or model load
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
                self._status.face_tracking_state = "requested" if enabled else "off"
                if cache is not None:
                    self._status.face_model_app_cache = cache
            # The daemon runs the tracker; returning here proves only that the command was sent.
            _LOGGER.info("Local face tracking %s", "requested (daemon-side, unconfirmed)" if enabled else "disabled")
            if cache is not None and cache != "verified":
                _LOGGER.warning(
                    "Pinned face model not verified in this app's cache (%s); see face-tracking docs", cache
                )
        except Exception as exc:
            self._face_tracking_active = False
            with self._status_lock:
                self._status.face_tracking_active = False
                self._status.face_tracking_state = "command_failed" if enabled else "off"
            _LOGGER.warning("Could not change local face tracking (%s)", type(exc).__name__)

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
