"""Wake-to-speech runtime for Reachy Mini Hermes."""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import secrets
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import cast

import httpx
import numpy as np

from .agent_audit import AgentAuditLog
from .agent_policy import AgentPolicy
from .audio import (
    AdaptiveEndpointRecorder,
    EndpointResult,
    NoiseFloor,
    encode_wav,
    mono_float32,
    resample_linear,
)
from .config import AppConfig, load_config
from .contextual_offers import ContextualOffer, ContextualOfferState, parse_offer_response
from .gesture_detection import GestureDetector, GestureReactionGate
from .hermes_client import AgentBrokerContext, HermesBridgeClient, HermesBridgeError, SpeechAudio
from .home_assistant import HermesHomeAssistantProvider, HomeAssistantBridge
from .initiative import (
    InitiativeCandidate,
    InitiativeDecision,
    InitiativeMode,
    InitiativePolicy,
    InitiativeSettings,
)
from .ispy import ISpyTarget
from .kids_mode import KidsProfile, build_kids_prompt, kids_greeting
from .motion import VoiceMotion
from .presence import PresenceObservation, PresenceState
from .presentation import IntentionalPresentationGate
from .realtime_client import RealtimeBridgeError, RealtimeBridgeSession
from .robot_tools import (
    CameraJoystickStream,
    ReachyRobotActions,
    completed_robot_tool_call,
    manual_precision_action,
    manual_robot_action,
)
from .wakeword import HeyHermesSpotter, ensure_kws_model

_LOGGER = logging.getLogger(__name__)
_POWER_MODES = frozenset({"standby", "awake", "meeting", "sleep"})
_MEDIA_TAG = re.compile(r"(?m)^\s*(?:\[\[audio_as_voice\]\]\s*)?MEDIA:\S+\s*$")
_MARKDOWN = re.compile(r"[`*_#>|]+")
_ANNOUNCEMENT_BEHAVIORS = frozenset({"voice_only", "wake_and_return", "wake_and_stay"})
_ANNOUNCEMENT_QUEUE_LIMIT = 20
_WAKE_PHRASES_TEXT = "Hey Hermes · Okay Nabu · Hey Reachy"
_WAKE_PROMPT = "Say “Hey Hermes”, “Okay Nabu”, or “Hey Reachy”"


@dataclass(slots=True)
class RuntimeStatus:
    state: str = "starting"
    detail: str = ""
    wake_word: str = _WAKE_PHRASES_TEXT
    transcript: str = ""
    response_preview: str = ""
    last_error: str = ""
    bridge_healthy: bool = False
    model_ready: bool = False
    turns_completed: int = 0
    stt_provider: str = ""
    tts_provider: str = ""
    audio_rms: float = 0.0
    audio_peak: float = 0.0
    audio_frames_processed: int = 0
    power_mode: str = "standby"
    meeting_seconds_remaining: int = 0
    interruptions: int = 0
    camera_captures: int = 0
    camera_last_error: str = ""
    face_tracking_active: bool = False
    doa_angle_degrees: float | None = None
    gesture_detection_active: bool = False
    gesture_detected: str = "none"
    gesture_confidence: float = 0.0
    gesture_frames_processed: int = 0
    gesture_reactions: int = 0
    gesture_last_action: str = ""
    gesture_last_error: str = ""
    robot_actions: int = 0
    last_robot_action: str = ""
    robot_action_last_error: str = ""
    announcement_busy: bool = False
    announcement_queue_depth: int = 0
    announcement_current_preview: str = ""
    announcement_last_text: str = ""
    announcement_last_error: str = ""
    announcement_provider: str = ""
    announcements_completed: int = 0


@dataclass(frozen=True, slots=True)
class Announcement:
    """One bounded text-to-speech announcement requested by the trusted UI."""

    text: str
    provider: str = ""
    model: str = ""
    voice: str = ""
    behavior: str = "wake_and_return"
    repeat: int = 1
    pause_seconds: float = 1.0
    cancellation_generation: int = 0
    contextual_offer_token: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)


@dataclass(slots=True)
class RealtimePlayback:
    """Track audio that may still be buffered after generation has finished."""

    item_id: str = ""
    started_at: float | None = None
    queued_until: float = 0.0
    duration_seconds: float = 0.0

    def add(self, now: float, duration_seconds: float) -> None:
        if self.started_at is None or now >= self.queued_until:
            self.started_at = now
            self.queued_until = now
            self.duration_seconds = 0.0
        self.duration_seconds += duration_seconds
        self.queued_until = max(now, self.queued_until) + duration_seconds

    def audible(self, now: float) -> bool:
        return self.started_at is not None and now < self.queued_until

    def played_ms(self, now: float) -> int:
        if self.started_at is None:
            return 0
        elapsed = max(0.0, now - self.started_at)
        return int(min(elapsed, self.duration_seconds) * 1000.0)

    def reset(self) -> None:
        self.item_id = ""
        self.started_at = None
        self.queued_until = 0.0
        self.duration_seconds = 0.0


def realtime_audio_item_id(kind: str, payload: dict[str, object]) -> str:
    """Return only an assistant message ID that can legally be audio-truncated."""
    if kind in {"response.output_audio.delta", "response.audio.delta"}:
        return str(payload.get("item_id") or "")
    if kind != "response.output_item.added":
        return ""
    item = payload.get("item")
    if not isinstance(item, dict):
        return ""
    if item.get("type") != "message" or item.get("role") != "assistant":
        return ""
    return str(item.get("id") or "")


def realtime_response_id(kind: str, payload: dict[str, object]) -> str:
    """Return the response owning an event so interrupted output can be dropped."""
    direct = str(payload.get("response_id") or "")
    if direct:
        return direct
    if kind in {"response.created", "response.done", "response.cancelled", "response.failed"}:
        response = payload.get("response")
        if isinstance(response, dict):
            return str(response.get("id") or "")
    return ""


@dataclass(frozen=True, slots=True)
class PowerModeToolCall:
    call_id: str
    mode: str
    duration_minutes: int | None


def completed_power_mode_call(
    kind: str,
    payload: dict[str, object],
) -> PowerModeToolCall | None:
    """Parse a local power request only after its Realtime call is completed."""
    if kind != "response.output_item.done":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    call_id = str(item.get("call_id") or "")
    if (
        item.get("type") != "function_call"
        or item.get("name") != "set_reachy_power_mode"
        or item.get("status") != "completed"
        or not call_id
    ):
        return None
    try:
        arguments = json.loads(item.get("arguments") or "{}")
    except (TypeError, json.JSONDecodeError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    mode = str(arguments.get("mode") or "").strip().lower()
    raw_duration = arguments.get("duration_minutes", 30)
    if isinstance(raw_duration, bool):
        duration_minutes = None
    else:
        try:
            duration_minutes = int(raw_duration)
        except (TypeError, ValueError):
            duration_minutes = None
    return PowerModeToolCall(call_id, mode, duration_minutes)


def completed_camera_call_id(kind: str, payload: dict[str, object]) -> str:
    """Return a completed camera tool call ID, never an in-progress/cancelled one."""
    if kind != "response.output_item.done":
        return ""
    item = payload.get("item")
    if not isinstance(item, dict):
        return ""
    if (
        item.get("type") != "function_call"
        or item.get("name") != "capture_reachy_camera"
        or item.get("status") != "completed"
    ):
        return ""
    return str(item.get("call_id") or "")


def doa_yaw_degrees(angle_radians: float) -> float:
    """Convert XVF3800 DOA coordinates to a conservative Reachy head yaw."""
    yaw = -(math.degrees(angle_radians) - 90.0) * 0.8
    if abs(yaw) < 10.0:
        return 0.0
    return round(max(-60.0, min(60.0, yaw)), 1)


class HermesVoiceRuntime:
    """Own microphone capture and serialize voice turns through Hermes."""

    def __init__(
        self,
        robot: object,
        stop_event: threading.Event,
        *,
        config_loader: Callable[[], AppConfig] = load_config,
        assets_directory: Path | None = None,
        agent_audit: AgentAuditLog | None = None,
    ) -> None:
        self.robot = robot
        self.stop_event = stop_event
        self.config_loader = config_loader
        self.assets = assets_directory or Path(__file__).resolve().parent / "assets"
        self._agent_audit = agent_audit
        self._status = RuntimeStatus()
        self._status_lock = threading.RLock()
        self._noise = NoiseFloor()
        self._sample_rate = 16000
        self._output_sample_rate = 48000
        self._motion: VoiceMotion | None = None
        self._actions: ReachyRobotActions | None = None
        self._spotter: HeyHermesSpotter | None = None
        self._last_wake_at = 0.0
        self._power_lock = threading.RLock()
        self._motor_transition_lock = threading.RLock()
        self._privacy_requested = threading.Event()
        self._conversation_stop_requested = threading.Event()
        # Every stop request advances this generation. A wake-started turn records it, so a
        # later "awake" transition that clears the event can never resume a stopped turn.
        self._conversation_stop_generation = 0
        self._turn_stop_generation = 0
        self._conversation_stop_generation_lock = threading.Lock()
        self._motors_enabled: bool | None = None
        self._head_safely_folded = False
        self._camera_lock = threading.Lock()
        self._camera_control_lock = threading.RLock()
        self._camera_control_session_id = ""
        self._camera_control_last_sequence = 0
        self._camera_control_last_activity = 0.0
        self._camera_control_stream: CameraJoystickStream | None = None
        self._power_mode = "standby"
        self._meeting_until = 0.0
        self._recording = False
        self._face_tracking_active = False
        self._face_tracking_desired = False
        self._face_tracking_weight = 0.65
        self._playback_stopped_for_privacy = False
        self._last_doa_sample_at = 0.0
        self._last_valid_doa_at = 0.0
        self._last_valid_doa_angle: float | None = None
        self._voice_activity_lock = threading.Lock()
        self._announcement_queue: queue.Queue[Announcement] = queue.Queue(maxsize=_ANNOUNCEMENT_QUEUE_LIMIT)
        self._announcement_state_lock = threading.RLock()
        self._announcement_cancellation_generation = 0
        self._voice_activity_generation = 0
        self._announcement_current: Announcement | None = None
        self._announcement_active = threading.Event()
        self._announcement_playing = threading.Event()
        self._announcement_worker: threading.Thread | None = None
        self._gesture_worker: threading.Thread | None = None
        self._gesture_stop_requested = threading.Event()
        self._presence = PresenceState()
        self._presence_action_active = threading.Event()
        self._initiative = InitiativePolicy()
        self._contextual_offers = ContextualOfferState()
        self._presentation_lock = threading.RLock()
        self._presentation_generation = 0
        self._presentation_state = "idle"
        self._presentation_reason = "none"
        self._presentation_started_at = 0.0
        self._presentation_expires_at = 0.0
        self._presentation_samples = 0
        self._presentation_detections = 0
        self._presentation_gate: IntentionalPresentationGate | None = None
        self._presentation_stop_requested = threading.Event()
        self._presentation_worker: threading.Thread | None = None
        self._audio_ready = False
        self._runtime_started = False
        self._control_ready = threading.Event()
        self._home_assistant_bridge: HomeAssistantBridge | None = None
        self._home_assistant_error = ""
        # Lock order: _motor_transition_lock before _kids_lock. status() reads Kids state and is
        # called while holding the motor lock, so the reverse nesting would deadlock.
        self._kids_lock = threading.RLock()
        self._kids_active = False
        self._kids_camera_active = False
        self._kids_locked = False
        self._kids_profile: KidsProfile | None = None
        self._kids_started_at = 0.0
        self._kids_ends_at = 0.0
        self._kids_session_id = ""
        self._kids_turns_at_start = 0
        self._kids_timer: threading.Timer | None = None
        self._kids_warning_timer: threading.Timer | None = None
        self._kids_generation = 0
        self._kids_ispy_player_motion_index = 0
        self._kids_last_end_reason = ""
        self._kids_last_fold_succeeded: bool | None = None
        self._agent_lock = threading.RLock()
        # Agent authority is never restored across process/session boundaries;
        # an unlocked adult UI must explicitly start each fresh Agent session.
        self._capability_profile = "conversation"
        # The companion bridge can outlive this app process.  Starting every
        # process at zero made a healthy restart look older than the bridge's
        # retained lease and permanently fail with ``stale_session``.  A
        # wall-clock nanosecond epoch preserves the existing +1 invalidation
        # semantics while ordering fresh app processes after their predecessors.
        self._agent_session_generation = time.time_ns()
        self._agent_current_task = ""
        self._agent_active_request_id = ""
        self._agent_pending_approval = False
        self._agent_activity: list[dict[str, object]] = []
        self._agent_policy = AgentPolicy()


    def _request_conversation_stop(self) -> None:
        """Stop the current voice turn; only a newly accepted wake may start another."""
        with self._conversation_stop_generation_lock:
            self._conversation_stop_generation += 1
            self._conversation_stop_requested.set()

    def _accept_wake_turn(self) -> None:
        """Begin a new wake-started turn that earlier stop requests no longer cancel."""
        with self._conversation_stop_generation_lock:
            self._conversation_stop_requested.clear()
            self._turn_stop_generation = self._conversation_stop_generation

    def _turn_stop_requested(self) -> bool:
        """Return whether the wake-started turn was stopped, even if the event was cleared since."""
        if self._conversation_stop_requested.is_set():
            return True
        with self._conversation_stop_generation_lock:
            return self._turn_stop_generation != self._conversation_stop_generation
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

    def status(self) -> dict[str, object]:
        with self._status_lock:
            payload = asdict(self._status)
        # Browser status is a low-trust polling surface. Conversation bodies
        # never belong in it, even briefly while a turn is in flight.
        payload.pop("transcript", None)
        payload.pop("response_preview", None)
        payload["robot_action_busy"] = bool(self._actions and self._actions.pending_count)
        payload["motors_enabled"] = self._motors_enabled
        payload["head_safely_folded"] = self._head_safely_folded
        with self._camera_control_lock:
            payload["camera_control_active"] = bool(self._camera_control_session_id)
        payload["announcement_queue_depth"] = self._announcement_queue.qsize()
        if self._home_assistant_bridge is not None:
            payload["home_assistant"] = self._home_assistant_bridge.status()
        else:
            payload["home_assistant"] = {
                "enabled": bool(self.config_loader().home_assistant_enabled),
                "ready": False,
                "connected": False,
                "error": self._home_assistant_error,
            }
        config = self.config_loader()
        payload["presence"] = self._presence.public_status(enabled=config.proactive_presence_enabled)
        initiative_status = self._initiative.public_status(self._initiative_settings(config))
        initiative_status["speech_enabled"] = bool(
            config.initiative_policy_enabled and config.contextual_offers_enabled
        )
        payload["initiative"] = initiative_status
        payload["contextual_offer"] = self._contextual_offers.public_status(
            enabled=config.initiative_policy_enabled and config.contextual_offers_enabled
        )
        payload["shared_physical_context"] = self._presentation_public_status(config)
        with self._agent_lock:
            payload["agent"] = self._agent_status_unlocked()
        with self._kids_lock:
            remaining = max(0, int(self._kids_ends_at - time.monotonic())) if self._kids_active else 0
            kids_payload = {
                "active": self._kids_active,
                "locked": self._kids_locked,
                "remaining_seconds": remaining,
                "profile": self._kids_profile.public_dict() if self._kids_profile else None,
                "turns_completed": (
                    max(0, int(payload["turns_completed"]) - self._kids_turns_at_start) if self._kids_active else 0
                ),
                "last_end_reason": self._kids_last_end_reason,
                "last_fold_succeeded": self._kids_last_fold_succeeded,
                "tool_policy": (
                    "voice-state-motion-only"
                    if self._kids_active and self._kids_profile and self._kids_profile.motion_enabled
                    else "no-tools"
                ),
                "camera_enabled": False,
                "camera_active": self._kids_camera_active,
            }
            payload["kids_mode"] = kids_payload
            if self._kids_locked:
                return {
                    "state": payload.get("state", "waiting_for_wake_word"),
                    "power_mode": payload.get("power_mode", "standby"),
                    "motors_enabled": self._motors_enabled,
                    "head_safely_folded": self._head_safely_folded,
                    "kids_mode": kids_payload,
                }
        return payload

    def _presentation_public_status(self, config: AppConfig | None = None) -> dict[str, object]:
        config = config or self.config_loader()
        enabled = bool(config.shared_physical_context_enabled)
        with self._presentation_lock:
            if not enabled:
                return {
                    "enabled": False,
                    "state": "disabled",
                    "reason": "disabled",
                    "visible_indicator": False,
                    "expires_seconds_remaining": 0,
                    "samples": 0,
                    "detections": self._presentation_detections,
                    "semantic_analysis": False,
                    "frames_retained": 0,
                }
            remaining = (
                max(0, int(math.ceil(self._presentation_expires_at - time.monotonic())))
                if self._presentation_state in {"starting", "watching"}
                else 0
            )
            return {
                "enabled": True,
                "state": self._presentation_state,
                "reason": self._presentation_reason,
                "visible_indicator": self._presentation_state in {"starting", "watching"},
                "expires_seconds_remaining": remaining,
                "samples": self._presentation_samples,
                "detections": self._presentation_detections,
                "semantic_analysis": False,
                "frames_retained": 0,
            }

    def _presentation_suppression_reason(self, config: AppConfig) -> str:
        if self.stop_event.is_set() or not self._control_ready.is_set() or not self._audio_ready:
            return "runtime is not ready"
        if not config.shared_physical_context_enabled:
            return "shared physical context is disabled"
        if not config.camera_enabled:
            return "camera access is disabled"
        if not config.contextual_offers_enabled or not config.initiative_policy_enabled:
            return "contextual offers are disabled"
        if self._effective_power_mode() != "awake" or self._motors_enabled is not True:
            return "Reachy must be safely Awake"
        if self._privacy_requested.is_set():
            return "privacy mode is active"
        with self._agent_lock:
            if self._capability_profile != "agent":
                return "Agent profile is required"
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                return "Kids Mode is active"
        with self._camera_control_lock:
            if self._camera_control_session_id:
                return "camera control is active"
        if self._announcement_active.is_set() or self._voice_activity_lock.locked():
            return "voice activity is active"
        if self._face_tracking_active:
            return "face tracking is active"
        actions = self._actions
        if actions is not None and (actions.busy or actions.pending_count):
            return "robot action is active"
        with self._status_lock:
            if self._status.last_error:
                return "runtime error is active"
            if self._status.state != "waiting_for_wake_word":
                return "voice activity is active"
        return ""

    def _presentation_window_active(self) -> bool:
        with self._presentation_lock:
            return self._presentation_state in {"starting", "watching"}

    def start_presentation_window(self) -> dict[str, object]:
        """Start one visible, local, non-semantic presentation window."""
        config = self.config_loader()
        reason = self._presentation_suppression_reason(config)
        if reason:
            raise RuntimeError(reason)
        duration = float(config.presentation_window_seconds)
        with self._presentation_lock:
            if self._presentation_state in {"starting", "watching"}:
                raise RuntimeError("A presentation window is already active")
            self._presentation_generation += 1
            generation = self._presentation_generation
            self._presentation_state = "starting"
            self._presentation_reason = "capturing_local_baseline"
            self._presentation_started_at = time.monotonic()
            self._presentation_expires_at = self._presentation_started_at + duration
            self._presentation_samples = 0
            self._presentation_stop_requested.clear()
            self._presentation_gate = None
        try:
            baseline = self._capture_camera_jpeg()
            gate = IntentionalPresentationGate(required_stable_frames=3)
            gate.begin(baseline)
            baseline = b""
        except Exception as exc:
            with self._presentation_lock:
                if generation == self._presentation_generation:
                    self._presentation_state = "error"
                    self._presentation_reason = "camera_unavailable"
                    self._presentation_expires_at = 0.0
                    self._presentation_gate = None
            raise RuntimeError(f"Could not start local presentation window: {exc}") from exc
        with self._presentation_lock:
            if generation != self._presentation_generation or self._presentation_stop_requested.is_set():
                gate.clear()
                raise RuntimeError("Presentation window was cancelled")
            self._presentation_gate = gate
            self._presentation_state = "watching"
            self._presentation_reason = "waiting_for_presented_object_or_text"
        self._start_presentation_worker(generation, gate, duration)
        return self._presentation_public_status(config)

    def _start_presentation_worker(
        self,
        generation: int,
        gate: IntentionalPresentationGate,
        duration: float,
    ) -> None:
        worker = threading.Thread(
            target=self._run_presentation_window,
            args=(generation, gate, duration),
            name="reachy-presentation",
            daemon=True,
        )
        with self._presentation_lock:
            self._presentation_worker = worker
        worker.start()

    def _run_presentation_window(
        self,
        generation: int,
        gate: IntentionalPresentationGate,
        duration: float,
    ) -> None:
        with self._presentation_lock:
            deadline = self._presentation_expires_at
        try:
            while not self._presentation_stop_requested.wait(0.5):
                if self.stop_event.is_set():
                    self.stop_presentation_window("runtime_stopping")
                    return
                with self._presentation_lock:
                    if generation != self._presentation_generation or self._presentation_state != "watching":
                        return
                if time.monotonic() >= deadline:
                    with self._presentation_lock:
                        if generation == self._presentation_generation:
                            self._presentation_state = "expired"
                            self._presentation_reason = "no_stable_presentation"
                            self._presentation_expires_at = 0.0
                            self._presentation_gate = None
                    return
                reason = self._presentation_suppression_reason(self.config_loader())
                if reason:
                    self.stop_presentation_window(reason.replace(" ", "_"))
                    return
                try:
                    jpeg = self._capture_camera_jpeg()
                    detected = self._observe_presentation_frame(generation, jpeg)
                    jpeg = b""
                except Exception:
                    _LOGGER.exception("Local presentation sampling failed")
                    self.stop_presentation_window("camera_error")
                    return
                if detected:
                    return
        finally:
            gate.clear()

    def _observe_presentation_frame(self, generation: int, jpeg: bytes) -> bool:
        with self._presentation_lock:
            if generation != self._presentation_generation or self._presentation_state != "watching":
                return False
            gate = self._presentation_gate
        if gate is None:
            return False
        try:
            detected = gate.observe(jpeg)
        except RuntimeError:
            with self._presentation_lock:
                if generation != self._presentation_generation or self._presentation_state != "watching":
                    return False
            raise
        if not detected:
            with self._presentation_lock:
                if generation == self._presentation_generation and self._presentation_state == "watching":
                    self._presentation_samples += 1
            return False
        with self._presentation_lock:
            if generation != self._presentation_generation or self._presentation_state != "watching":
                return False
            self._presentation_samples += 1
            self._presentation_detections += 1
            self._presentation_state = "detected"
            self._presentation_reason = "stable_presented_change"
            self._presentation_expires_at = 0.0
            self._presentation_gate = None
            self._presentation_stop_requested.set()
        offer = ContextualOffer(
            source="presentation",
            topic="presented_context",
            confidence=0.95,
            fingerprint=f"presentation-{generation}",
            text="I can see you are showing me something; would you like help looking at or reading it?",
            accepted_text="Okay—say Hey Hermes and ask me to look at what you are showing me.",
        )
        try:
            result = self.submit_contextual_offer(offer)
            reason = "offer_queued" if result.get("queued") is True else str(result.get("reason") or "suppressed")
        except (ValueError, RuntimeError) as exc:
            reason = f"offer_suppressed:{str(exc)[:80]}"
        with self._presentation_lock:
            if generation == self._presentation_generation:
                self._presentation_reason = reason
        return True

    def stop_presentation_window(self, reason: str = "user_stopped") -> dict[str, object]:
        """Cancel local sampling and discard every ephemeral visual feature."""
        with self._presentation_lock:
            active = self._presentation_state in {"starting", "watching"}
            self._presentation_generation += 1
            self._presentation_stop_requested.set()
            gate = self._presentation_gate
            self._presentation_gate = None
            self._presentation_expires_at = 0.0
            if active:
                self._presentation_state = "cancelled"
                self._presentation_reason = reason[:96] or "cancelled"
            if gate is not None:
                gate.clear()
        return self._presentation_public_status()

    def _presence_suppression_reason(
        self,
        config: AppConfig,
        *,
        owns_voice_activity: bool = False,
        owns_announcement: bool = False,
    ) -> str:
        """Return why silent presence motion is currently unsafe, or an empty string."""
        if self.stop_event.is_set() or not self._control_ready.is_set() or not self._audio_ready:
            return "runtime_not_ready"
        power_mode = self._effective_power_mode()
        if power_mode in {"meeting", "sleep"}:
            return power_mode
        if self._privacy_requested.is_set():
            return "privacy"
        if power_mode != "awake":
            return "not_awake"
        if self._motors_enabled is not True:
            return "motors_not_enabled"
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                return "kids_mode"
        if self._announcement_active.is_set() and not owns_announcement:
            return "announcement_active"
        if self._voice_activity_lock.locked() and not owns_voice_activity:
            return "voice_active"
        with self._status_lock:
            if self._status.last_error or self._status.state in {
                "starting",
                "stopping",
                "configuration_error",
                "power_transition_error",
            }:
                return "runtime_error"
            if self._status.state != "waiting_for_wake_word":
                return "voice_active"
        with self._camera_control_lock:
            if self._camera_control_session_id:
                return "camera_control_active"
        if self._face_tracking_active:
            return "face_tracking_active"
        actions = self._actions
        if actions is not None and (actions.busy or actions.pending_count):
            return "robot_action_active"
        if self._motion is None or not config.motion_enabled:
            return "motion_disabled"
        return ""

    def observe_presence(
        self,
        observation: PresenceObservation,
        *,
        allow_acknowledgement: bool = True,
    ) -> dict[str, object]:
        """Record one bounded signal and optionally perform one safe, silent acknowledgement."""
        config = self.config_loader()
        if not config.proactive_presence_enabled:
            self._presence.clear("disabled")
            return self._presence.public_status(enabled=False)
        self._presence.observe(observation)
        if not observation.occupied:
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "away")
            return self._presence.public_status(enabled=True)
        if not allow_acknowledgement:
            self._presence.record_suppression(f"observed_{observation.source}")
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "observation_only")
            return self._presence.public_status(enabled=True)
        if not config.presence_acknowledgement_enabled:
            self._presence.record_suppression("acknowledgement_disabled")
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "acknowledgement_disabled")
            return self._presence.public_status(enabled=True)
        if not self._presence.acknowledgement_due(config.presence_acknowledgement_cooldown_seconds):
            self._presence.record_suppression("cooldown")
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "cooldown")
            return self._presence.public_status(enabled=True)

        reason = self._presence_suppression_reason(config)
        initiative_decision: InitiativeDecision | None = None
        if config.initiative_policy_enabled:
            initiative_decision = self._evaluate_presence_initiative(observation, config, reason)
            if initiative_decision.outcome == "remain_silent":
                self._presence.record_suppression(initiative_decision.reason)
                return self._presence.public_status(enabled=True)
        if reason:
            self._presence.record_suppression(reason)
            return self._presence.public_status(enabled=True)
        if not self._voice_activity_lock.acquire(blocking=False):
            if initiative_decision is not None:
                self._initiative.cancel(initiative_decision, "voice_active")
            self._presence.record_suppression("voice_active")
            return self._presence.public_status(enabled=True)
        try:
            # Recheck after acquiring the voice owner slot so a concurrent wake,
            # power transition, Kids transition, or explicit action cannot race.
            with self._motor_transition_lock:
                with self._kids_lock:
                    reason = self._presence_suppression_reason(config, owns_voice_activity=True)
                    actions = self._actions
                    if reason:
                        if initiative_decision is not None:
                            self._initiative.cancel(initiative_decision, reason)
                        self._presence.record_suppression(reason)
                    elif actions is None:
                        if initiative_decision is not None:
                            self._initiative.cancel(initiative_decision, "runtime_not_ready")
                        self._presence.record_suppression("runtime_not_ready")
                    else:
                        self._presence_action_active.set()
                        arguments: dict[str, object] = {}
                        if observation.direction_degrees is not None:
                            arguments["direction_degrees"] = observation.direction_degrees
                        queued = actions.enqueue(
                            "acknowledge_presence",
                            arguments,
                            hold_pose=True,
                            reject_if_busy=True,
                        )
                        if queued.get("accepted"):
                            self._presence.reserve_acknowledgement()
                            if initiative_decision is not None:
                                self._initiative.commit(initiative_decision)
                        else:
                            self._presence_action_active.clear()
                            queue_reason = str(queued.get("code") or "motion_failed")
                            if initiative_decision is not None:
                                self._initiative.cancel(initiative_decision, queue_reason)
                            self._presence.record_suppression(queue_reason)
        finally:
            self._voice_activity_lock.release()
        return self._presence.public_status(enabled=True)

    @staticmethod
    def _initiative_settings(config: AppConfig) -> InitiativeSettings:
        return InitiativeSettings(
            enabled=config.initiative_policy_enabled,
            mode=cast(InitiativeMode, config.initiative_mode),
            quiet_hours_enabled=config.initiative_quiet_hours_enabled,
            quiet_hours_start=config.initiative_quiet_hours_start,
            quiet_hours_end=config.initiative_quiet_hours_end,
            hourly_budget=config.initiative_hourly_budget,
            daily_budget=config.initiative_daily_budget,
            topic_cooldown_seconds=config.initiative_topic_cooldown_seconds,
            duplicate_window_seconds=config.initiative_duplicate_window_seconds,
            dismissal_backoff_seconds=config.initiative_dismissal_backoff_seconds,
        )

    def _evaluate_presence_initiative(
        self,
        observation: PresenceObservation,
        config: AppConfig,
        suppression_reason: str,
    ) -> InitiativeDecision:
        return self._initiative.evaluate(
            InitiativeCandidate(
                topic="office_presence",
                requested_outcome="physical_acknowledgement",
                confidence=observation.confidence,
                attentive=observation.attentive,
                fingerprint=f"presence:{observation.source}:{'attentive' if observation.attentive else 'present'}",
            ),
            self._initiative_settings(config),
            suppression_reason=suppression_reason,
        )

    def evaluate_initiative_candidate(
        self,
        candidate: InitiativeCandidate,
        *,
        commit: bool = False,
    ) -> InitiativeDecision:
        """Evaluate a sanitized future offer without generating or speaking content."""
        config = self.config_loader()
        decision = self._initiative.evaluate(
            candidate,
            self._initiative_settings(config),
            suppression_reason=self._presence_suppression_reason(config),
        )
        if commit and decision.outcome != "remain_silent":
            self._initiative.commit(decision)
        return decision

    def submit_contextual_offer(self, offer: ContextualOffer) -> dict[str, object]:
        """Evaluate and queue one read-only spoken offer from trusted structured context."""
        config = self.config_loader()
        with self._agent_lock:
            agent_active = self._capability_profile == "agent"
        suppression_reason = self._presence_suppression_reason(config)
        if not config.contextual_offers_enabled:
            suppression_reason = suppression_reason or "contextual_offers_disabled"
        elif not agent_active:
            suppression_reason = suppression_reason or "agent_profile_inactive"
        decision = self._initiative.evaluate(
            InitiativeCandidate(
                topic=offer.topic,
                requested_outcome="offer_candidate",
                confidence=offer.confidence,
                fingerprint=offer.fingerprint,
            ),
            self._initiative_settings(config),
            suppression_reason=suppression_reason,
        )
        if decision.outcome != "offer_candidate":
            return {
                "ok": True,
                "queued": False,
                "decision": decision.outcome,
                "reason": decision.reason,
            }
        token = 0
        try:
            token = self._contextual_offers.queue(
                offer,
                response_window_seconds=config.contextual_offer_response_window_seconds,
            )
            queued = self.queue_announcement(
                offer.text,
                behavior="voice_only",
                contextual_offer_token=token,
            )
        except Exception:
            if token:
                self._contextual_offers.cancel(token, "queue_failed")
            self._initiative.cancel(decision, "queue_failed")
            raise
        if not self._initiative.commit(decision):
            self._contextual_offers.cancel(token, "stale_decision")
            raise RuntimeError("Contextual offer eligibility became stale")
        return {
            **queued,
            "decision": "offer_candidate",
            "reason": "committed",
            "token": token,
        }

    def respond_to_contextual_offer(self, token: int, response: str) -> dict[str, object]:
        """Record one exact phone/voice response without performing a consequential action."""
        result = self._contextual_offers.respond(token, response)
        offer = self._contextual_offers.current_offer(token)
        config = self.config_loader()
        if response == "yes":
            self._initiative.record_welcomed(offer.topic)
            self.queue_announcement(str(result["accepted_text"]), behavior="voice_only")
        else:
            self._initiative.record_dismissal(offer.topic, self._initiative_settings(config))
        return {"ok": True, "token": token, "response": response, "action_executed": False}

    def cancel_contextual_offer(self, reason: str = "disabled") -> bool:
        """Cancel only the active contextual offer, preserving unrelated announcements."""
        token = self._contextual_offers.cancel_active(reason)
        if not token:
            return False
        stop_playback = False
        with self._announcement_state_lock:
            current = self._announcement_current
            if current is not None and current.contextual_offer_token == token:
                current.cancel_event.set()
                stop_playback = self._announcement_playing.is_set()
        if stop_playback:
            try:
                play_sound = getattr(getattr(self.robot, "media", None), "play_sound", None)
                if callable(play_sound):
                    play_sound(str(self.assets / "silence.wav"))
            except Exception:
                _LOGGER.debug("Could not stop contextual-offer playback", exc_info=True)
            self._clear_streamed_audio()
        return True

    @property
    def control_ready(self) -> bool:
        """Return whether external controls may begin hardware transitions."""
        return self._control_ready.is_set()

    def set_capability_profile(self, profile: str, *, adult_ui_unlocked: bool) -> dict[str, object]:
        """Switch profiles only from the unlocked adult UI and start a fresh generation."""
        profile = profile.strip().lower()
        if profile not in {"conversation", "agent"}:
            raise ValueError("Unsupported capability profile")
        if profile == "agent":
            if not adult_ui_unlocked:
                raise RuntimeError("Agent profile requires an unlocked adult UI action")
            if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
                raise RuntimeError("Agent profile is blocked by the current privacy mode")
        # Profile/Kids transitions always acquire _kids_lock before _agent_lock.
        # Holding both makes the mutual-exclusion check and profile mutation atomic.
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                raise RuntimeError("Agent profile is unavailable while Kids Mode is active or locked")
            with self._agent_lock:
                active_request_id = self._agent_active_request_id
                self._agent_session_generation += 1
                self._capability_profile = profile
                self._agent_current_task = ""
                self._agent_active_request_id = ""
                self._agent_pending_approval = False
                self._record_agent_activity_unlocked("profile_changed")
                payload = self._agent_status_unlocked()
        self._request_conversation_stop()
        if profile != "agent":
            self.cancel_contextual_offer("agent_profile_inactive")
            self.stop_presentation_window("agent_profile_inactive")
        if active_request_id:
            threading.Thread(
                target=self._cancel_remote_agent_request,
                args=(active_request_id,),
                name="reachy-agent-cancel",
                daemon=True,
            ).start()
        self._publish_remote_agent_session()
        return payload

    def cancel_agent_work(self, reason: str = "stopped") -> dict[str, object]:
        """Invalidate pending Agent work and stop voice only for teardown reasons."""
        allowed_reasons = {
            "stopped",
            "profile_changed",
            "power_standby",
            "power_meeting",
            "power_sleep",
            "kids_mode",
            "privacy",
            "emergency_stop",
            "session_changed",
        }
        # Only the internal, explicit session_changed reason may preserve the
        # voice loop. Unknown callers fail closed as a normal Stop request.
        safe_reason = reason if reason in allowed_reasons else "stopped"
        with self._agent_lock:
            active_request_id = self._agent_active_request_id
            self._agent_session_generation += 1
            self._agent_current_task = ""
            self._agent_active_request_id = ""
            self._agent_pending_approval = False
            self._record_agent_activity_unlocked(safe_reason)
            payload = self._agent_status_unlocked()
        # A newly accepted wake advances the Agent generation so stale tool
        # results cannot cross into the new voice session. It is not itself a
        # Stop request. In particular, do not clear the event here: a real
        # Stop/privacy/power/Kids cancellation may have raced with this call.
        if safe_reason != "session_changed":
            self._request_conversation_stop()
        if active_request_id:
            threading.Thread(
                target=self._cancel_remote_agent_request,
                args=(active_request_id,),
                name="reachy-agent-cancel",
                daemon=True,
            ).start()
        self._publish_remote_agent_session()
        return payload

    def _cancel_remote_agent_request(self, request_id: str) -> None:
        client = HermesBridgeClient(self.config_loader())
        try:
            client.cancel_agent_request(request_id)
        except Exception:
            _LOGGER.warning("Could not confirm remote Agent request cancellation", exc_info=True)
        finally:
            client.close()

    def _publish_remote_agent_session(self) -> None:
        """Best-effort invalidation of the Hermes-hosted device lease."""
        config = self.config_loader()
        if not config.api_key:
            return
        context = self.agent_broker_context(explicit_private_intent=False)

        def publish() -> None:
            client = HermesBridgeClient(config)
            try:
                client.establish_agent_session(context)
            except Exception:
                _LOGGER.warning("Could not publish Agent session generation", exc_info=True)
            finally:
                client.close()

        threading.Thread(target=publish, name="reachy-agent-session", daemon=True).start()

    def _establish_remote_agent_session(self, context: AgentBrokerContext) -> None:
        client = HermesBridgeClient(self.config_loader())
        try:
            client.establish_agent_session(context)
        finally:
            client.close()

    def agent_broker_context(self, *, explicit_private_intent: bool) -> AgentBrokerContext:
        """Capture one sanitized, generation-bound broker authorization context."""
        with self._kids_lock:
            kids_active = self._kids_active or self._kids_locked
            with self._agent_lock:
                profile = self._capability_profile
                generation = self._agent_session_generation
        with self._status_lock:
            state = self._status.state
            detail = self._status.detail
            bridge_healthy = self._status.bridge_healthy
            last_error = self._status.last_error
        power_mode = self._effective_power_mode()
        return AgentBrokerContext(
            capability_profile=profile,
            adult_ui_unlocked=profile == "agent",
            kids_mode_active=kids_active,
            power_mode=power_mode,
            privacy_enabled=not self._privacy_requested.is_set(),
            emergency_stop_active=False,
            robot_available=not self.stop_event.is_set() and self._audio_ready,
            session_generation=generation,
            requested_session_generation=generation,
            explicit_private_intent=explicit_private_intent,
            reachy_status={
                "observed_at": time.time(),
                "state": state,
                "detail": detail,
                "power_mode": power_mode,
                "motors_enabled": self._motors_enabled,
                "head_safely_folded": self._head_safely_folded,
                "bridge_healthy": bridge_healthy,
                "robot_action_busy": bool(self._actions and self._actions.pending_count),
                "last_error": last_error,
            },
        )

    def _begin_agent_request(self, _summary: str) -> tuple[str, AgentBrokerContext]:
        # The bridge derives T1 intent from this exact request and capability;
        # the robot never grants blanket private-read intent for an Agent session.
        context = self.agent_broker_context(explicit_private_intent=False)
        request_id = f"agent-{secrets.token_hex(16)}"
        self._establish_remote_agent_session(context)
        with self._agent_lock:
            if context.session_generation != self._agent_session_generation or self._capability_profile != "agent":
                raise RuntimeError("Agent session changed before the request started")
            if self._agent_active_request_id:
                raise RuntimeError("Another Agent request is already active")
            self._agent_active_request_id = request_id
            # Browser status is a low-trust surface: never mirror a transcript,
            # query, note name, token, or other request content into it.
            self._agent_current_task = "Processing a bounded owner request"
            self._record_agent_activity_unlocked("request_started")
        return request_id, context

    def _finish_agent_request(self, request_id: str, generation: int, *, succeeded: bool) -> bool:
        with self._agent_lock:
            current = generation == self._agent_session_generation and self._agent_active_request_id == request_id
            if not current:
                return False
            self._agent_active_request_id = ""
            self._agent_current_task = ""
            self._record_agent_activity_unlocked("request_completed" if succeeded else "request_failed")
            return True

    def agent_session_is_current(self, generation: int) -> bool:
        with self._agent_lock:
            return generation == self._agent_session_generation

    def record_agent_run_event(self, action: str, run: dict[str, object]) -> None:
        """Record only run identity/status; never persist goals, arguments, or results."""
        run_id = str(run.get("run_id") or "")[:32]
        status = str(run.get("status") or "unknown")[:32]
        with self._agent_lock:
            self._agent_activity.append(
                {
                    "event": f"run_{action}"[:48],
                    "generation": self._agent_session_generation,
                    "run_id": run_id,
                    "result_class": status,
                }
            )
            del self._agent_activity[:-20]
            if self._agent_audit is not None:
                self._agent_audit.append(
                    "agent_run",
                    reason=action,
                    session_generation=self._agent_session_generation,
                    result_class=status,
                    summary=f"{run_id}:{status}",
                )

    def _record_agent_activity_unlocked(self, event: str) -> None:
        self._agent_activity.append({"event": event, "generation": self._agent_session_generation})
        del self._agent_activity[:-20]
        if self._agent_audit is not None:
            result_classes = {
                "profile_changed": "profile_updated",
                "request_started": "running",
                "request_completed": "success",
                "request_failed": "failed",
            }
            self._agent_audit.append(
                "agent_session",
                reason=event,
                session_generation=self._agent_session_generation,
                result_class=result_classes.get(event, "cancelled"),
            )

    def _agent_status_unlocked(self) -> dict[str, object]:
        return {
            "profile": self._capability_profile,
            "session_generation": self._agent_session_generation,
            "enabled_capabilities": [
                definition.capability_id.value for definition in self._agent_policy.enabled_capabilities()
            ]
            if self._capability_profile == "agent"
            else [],
            "current_task": self._agent_current_task,
            "pending_approval": self._agent_pending_approval,
            "recent_activity": [dict(item) for item in self._agent_activity[-20:]],
        }

    @property
    def kids_controls_locked(self) -> bool:
        # Read without _kids_lock: the async HTTP middleware calls this on the event loop, and
        # _kids_lock can be held across multi-second robot transitions. Writers still hold the
        # lock; a single attribute read is atomic.
        return self._kids_locked

    def start_kids_mode(self, profile: KidsProfile, *, greet: bool = True) -> dict[str, object]:
        """Start one time-bounded, camera-free, private-tool-free Realtime session."""
        self.stop_presentation_window("kids_mode")
        if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
            raise RuntimeError("Kids Mode is blocked in Meeting and Sleep")
        if not self._audio_ready:
            raise RuntimeError("Kids Mode audio is not ready")
        self.revoke_camera_control()
        with self._kids_lock:
            # Keep the Kids guard while invalidating Agent work and forcing the
            # conversation profile, matching set_capability_profile's lock order.
            self.cancel_agent_work("kids_mode")
            with self._agent_lock:
                self._capability_profile = "conversation"
            previous = self._kids_timer
            previous_warning = self._kids_warning_timer
            replaced_session_id = self._kids_session_id if self._kids_active else ""
            if previous is not None:
                previous.cancel()
            if previous_warning is not None:
                previous_warning.cancel()
            self._kids_generation += 1
            generation = self._kids_generation
            self._kids_ispy_player_motion_index = 0
            self._kids_active = True
            self._kids_camera_active = False
            self._kids_locked = True
            self._presence.clear("kids_mode")
            self._kids_profile = profile
            self._kids_started_at = time.monotonic()
            self._kids_ends_at = self._kids_started_at + profile.duration_minutes * 60.0
            self._kids_session_id = "kids-" + secrets.token_hex(16)
            with self._status_lock:
                self._kids_turns_at_start = self._status.turns_completed
            self._kids_last_end_reason = ""
            self._kids_last_fold_succeeded = None
            timer = threading.Timer(profile.duration_minutes * 60.0, self._expire_kids_mode, args=(generation,))
            timer.daemon = True
            self._kids_timer = timer
            warning = threading.Timer(
                max(1.0, profile.duration_minutes * 60.0 - 300.0),
                self._warn_kids_mode,
                args=(generation,),
            )
            warning.daemon = True
            self._kids_warning_timer = warning
            timer.start()
            warning.start()
            started_session_id = self._kids_session_id
        if replaced_session_id:
            self._notify_bridge_kids_session_async(replaced_session_id, active=False)
        self._notify_bridge_kids_session_async(started_session_id, active=True)
        self._request_conversation_stop()
        with self._status_lock:
            self._status.transcript = ""
            self._status.response_preview = ""
        if self._motion is not None:
            self._motion.enabled = profile.motion_enabled
        ispy_target: ISpyTarget | None = None
        if profile.activity == "ispy":
            try:
                ispy_target = self._prepare_ispy_round(generation, profile)
            except Exception as exc:
                _LOGGER.warning("I Spy start failed (%s): %s", type(exc).__name__, exc)
                if self._kids_callback_is_current(generation):
                    self.stop_kids_mode(reason="ispy_start_failed", fold=True)
                raise
        if greet:
            try:
                if profile.activity == "ispy" and not self._kids_callback_is_current(generation):
                    raise RuntimeError("I Spy start was cancelled before its clue")
                self.queue_announcement(
                    ispy_target.clue(profile.language) if ispy_target else kids_greeting(profile),
                    behavior="wake_and_stay",
                    provider="elevenlabs",
                    model="eleven_flash_v2_5",
                    voice="cgSgspJ2msm6clMCkdW9",
                )
            except Exception:
                self.stop_kids_mode(reason="start_failed", fold=False)
                raise
        _LOGGER.info(
            "Kids Mode started (age=%s activity=%s duration=%sm motion=%s)",
            profile.age_band,
            profile.activity,
            profile.duration_minutes,
            profile.motion_enabled,
        )
        return dict(self.status()["kids_mode"])  # type: ignore[arg-type]

    @staticmethod
    def _ispy_head_target(yaw_degrees: float, pitch_degrees: float) -> np.ndarray:
        yaw, pitch = math.radians(yaw_degrees), math.radians(pitch_degrees)
        cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
        return np.array(
            [
                [cy * cp, -sy, cy * sp, 0.0],
                [sy * cp, cy, sy * sp, 0.0],
                [-sp, 0.0, cp, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )

    def _prepare_ispy_round(
        self,
        generation: int,
        profile: KidsProfile,
        *,
        client: HermesBridgeClient | None = None,
    ) -> ISpyTarget:
        """Run one consented, cancellable search; frames exist only on this stack."""
        if not profile.camera_consent:
            raise RuntimeError("Camera opt-in is required for I Spy")
        session_id = self._kids_session_id
        self.set_power_mode("awake", cancel_announcements=False)
        # Five retained viewpoints cover a 240° desk arc while every commanded segment remains <= 60°.
        # Two intermediate waypoints cross safely from the left extreme to the right side without capturing.
        scan_route = (
            (0.0, True),
            (-60.0, True),
            (-120.0, True),
            (-60.0, False),
            (0.0, False),
            (60.0, True),
            (120.0, True),
        )
        return_route = (60.0, 0.0)
        frames: list[bytes] = []
        owns_client = client is None
        goto_target = getattr(self.robot, "goto_target", None)
        if not callable(goto_target):
            raise RuntimeError("I Spy search motion is unavailable")
        with self._kids_lock:
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy search was cancelled")
            self._kids_camera_active = True
        try:
            with self._motor_transition_lock:
                previous_body_yaw = 0.0
                for body_yaw, capture in scan_route:
                    if not self._kids_callback_is_current(generation):
                        raise RuntimeError("I Spy search was cancelled")
                    angular_distance = abs(body_yaw - previous_body_yaw)
                    goto_target(
                        head=self._ispy_head_target(body_yaw, -5.0),
                        body_yaw=math.radians(body_yaw),
                        antennas=np.radians(np.asarray([8.0, -8.0])),
                        duration=max(0.6, angular_distance / 30.0),
                        method="minjerk",
                    )
                    previous_body_yaw = body_yaw
                    if capture:
                        frames.append(self._capture_camera_jpeg(kids_generation=generation))
                with self._kids_lock:
                    self._kids_camera_active = False
                for body_yaw in return_route:
                    if not self._kids_callback_is_current(generation):
                        raise RuntimeError("I Spy search was cancelled")
                    angular_distance = abs(body_yaw - previous_body_yaw)
                    goto_target(
                        head=self._ispy_head_target(body_yaw, -5.0 if body_yaw else 0.0),
                        body_yaw=math.radians(body_yaw),
                        antennas=np.asarray([0.0, 0.0]),
                        duration=max(0.6, angular_distance / 30.0),
                        method="minjerk",
                    )
                    previous_body_yaw = body_yaw
            with self._kids_lock:
                self._kids_camera_active = False
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy search was cancelled")
            if client is None:
                client = HermesBridgeClient(self.config_loader())
            target = client.select_ispy_target(
                frames,
                session_id=session_id,
                age_band=profile.age_band,
                language=profile.language,
            )
            if not self._kids_callback_is_current(generation):
                try:
                    client.cancel_ispy_session(session_id)
                except Exception:
                    _LOGGER.warning("Could not delete a cancelled I Spy bridge session")
                raise RuntimeError("I Spy search was cancelled")
            with self._kids_lock:
                self._kids_ispy_player_motion_index = 0
            return target
        finally:
            with self._kids_lock:
                self._kids_camera_active = False
            frames.clear()
            if owns_client and client is not None:
                client.close()

    def _perform_ispy_player_guess_motion(self, generation: int) -> None:
        """Use both the base and head for each player-object guess, without camera access."""
        goto_target = getattr(self.robot, "goto_target", None)
        if not callable(goto_target):
            raise RuntimeError("I Spy player-turn motion is unavailable")
        with self._kids_lock:
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy player-turn motion was cancelled")
            index = self._kids_ispy_player_motion_index
            self._kids_ispy_player_motion_index += 1
        body_yaws = (-12.0, 12.0, -8.0, 8.0, -5.0, 5.0)
        body_yaw = body_yaws[index % len(body_yaws)]
        head_yaw = body_yaw * 1.5
        with self._motor_transition_lock:
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy player-turn motion was cancelled")
            goto_target(
                head=self._ispy_head_target(head_yaw, -4.0),
                body_yaw=math.radians(body_yaw),
                antennas=np.radians(np.asarray([5.0, -5.0])),
                duration=1.5,
                method="minjerk",
            )
        if not self._kids_callback_is_current(generation):
            raise RuntimeError("I Spy player-turn motion was cancelled")

    def _try_ispy_player_guess_motion(self, generation: int) -> bool:
        """Keep an optional embodied gesture from suppressing an approved guess."""
        try:
            self._perform_ispy_player_guess_motion(generation)
            return True
        except Exception:
            _LOGGER.warning("I Spy player-picker gesture failed; continuing with speech", exc_info=True)
            return False

    def _continue_ispy_reachy_turn(
        self,
        client: HermesBridgeClient,
        *,
        barge_in: bool,
    ) -> bool:
        """Search with base motion, select a fresh target, and speak its approved clue."""
        with self._kids_lock:
            generation = self._kids_generation
            profile = self._kids_profile
            session_id = self._kids_session_id
            if (
                not self._kids_active
                or profile is None
                or profile.activity != "ispy"
                or client.config.kids_session_id != session_id
            ):
                raise RuntimeError("I Spy turn continuation is stale")
        try:
            self._prepare_ispy_round(generation, profile, client=client)
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy turn continuation was cancelled")
            clue = client.ispy_clue(session_id)
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy clue was cancelled")
            self._set_status(
                "speaking",
                "Streaming the next I Spy colour clue",
                tts_provider="elevenlabs-flash-stream",
            )
            return self._play_kids_stream(client, clue, barge_in=barge_in)
        except Exception:
            self.stop_kids_mode(reason="ispy_round_failed", fold=True)
            raise

    def _kids_callback_is_current(self, generation: int) -> bool:
        with self._kids_lock:
            return self._kids_active and generation == self._kids_generation

    def _warn_kids_mode(self, generation: int) -> None:
        with self._kids_lock:
            if not self._kids_active or generation != self._kids_generation:
                return
        try:
            self._request_conversation_stop()
            self._clear_streamed_audio()
            self.queue_announcement(
                text="Five minutes left in Kids Mode. Let's finish this activity soon.",
                repeat=1,
                pause_seconds=0.0,
                behavior="voice_only",
                provider="elevenlabs",
                model="eleven_flash_v2_5",
                voice="cgSgspJ2msm6clMCkdW9",
            )
        except Exception:
            _LOGGER.exception("Could not play the Kids Mode five-minute warning")

    def _expire_kids_mode(self, generation: int) -> None:
        with self._kids_lock:
            if not self._kids_active or generation != self._kids_generation:
                return
        try:
            self.stop_kids_mode(reason="time_limit", fold=True)
        except Exception:
            _LOGGER.exception("Kids Mode expiry could not complete the safe fold")

    def _notify_bridge_kids_session(self, session_id: str, active: bool) -> None:
        client = HermesBridgeClient(self.config_loader())
        try:
            client.set_kids_session_state(session_id, active=active)
        except Exception:
            _LOGGER.warning("Could not report the Kids session state to the Hermes bridge")
        finally:
            client.close()

    def _notify_bridge_kids_session_async(self, session_id: str, active: bool) -> None:
        threading.Thread(
            target=self._notify_bridge_kids_session,
            args=(session_id, active),
            name="kids-bridge-session",
            daemon=True,
        ).start()

    def _cancel_ispy_bridge_session(self, session_id: str) -> None:
        client = HermesBridgeClient(self.config_loader())
        try:
            client.cancel_ispy_session(session_id)
        except Exception:
            _LOGGER.warning("Could not delete stopped I Spy bridge session")
        finally:
            client.close()

    def stop_kids_mode(self, *, reason: str = "parent", fold: bool = True) -> dict[str, object]:
        """End Kids Mode immediately, cancel its voice/motion, and optionally fold safely."""
        with self._kids_lock:
            was_active = self._kids_active
            ended_session_id = self._kids_session_id if was_active else ""
            ispy_session_id = (
                self._kids_session_id
                if self._kids_profile is not None and self._kids_profile.activity == "ispy"
                else ""
            )
            timer, self._kids_timer = self._kids_timer, None
            warning, self._kids_warning_timer = self._kids_warning_timer, None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            if warning is not None and warning is not threading.current_thread():
                warning.cancel()
            self._kids_generation += 1
            self._kids_active = False
            self._kids_camera_active = False
            self._kids_locked = False
            self._kids_profile = None
            self._kids_ends_at = 0.0
            self._kids_session_id = ""
            self._kids_last_end_reason = reason[:40]
            self._kids_last_fold_succeeded = None
        if ended_session_id:
            self._notify_bridge_kids_session_async(ended_session_id, active=False)
        self._request_conversation_stop()
        self._cancel_announcements(clear_queue=True)
        self._clear_streamed_audio()
        cancel_move = getattr(self.robot, "cancel_move", None)
        if was_active and callable(cancel_move):
            try:
                cancel_move()
            except Exception:
                _LOGGER.warning("Could not cancel the active Kids Mode movement", exc_info=True)
        if ispy_session_id:
            cancel_thread = threading.Thread(
                target=self._cancel_ispy_bridge_session,
                args=(ispy_session_id,),
                name="kids-ispy-cancel",
                daemon=True,
            )
            cancel_thread.start()
        with self._status_lock:
            self._status.transcript = ""
            self._status.response_preview = ""
        if self._motion is not None:
            try:
                self._motion.enabled = self.config_loader().motion_enabled
            except Exception:
                self._motion.enabled = False
        if self._actions is not None:
            self._actions.cancel(stop_media=False)
        if fold:
            try:
                if self._effective_power_mode() not in {"meeting", "sleep"}:
                    self.set_power_mode("standby", cancel_announcements=False)
                if not self._head_safely_folded or self._motors_enabled is not False:
                    raise RuntimeError("Kids Mode ended, but Reachy's safe fold could not be verified")
            except Exception:
                with self._kids_lock:
                    self._kids_last_fold_succeeded = False
                    self._kids_last_end_reason = f"{reason[:28]}_fold_failed"
                raise
            with self._kids_lock:
                self._kids_last_fold_succeeded = True
        _LOGGER.info("Kids Mode ended (reason=%s, was_active=%s)", reason, was_active)
        return dict(self.status()["kids_mode"])  # type: ignore[arg-type]

    def _kids_voice_config(self, base: AppConfig) -> AppConfig:
        with self._kids_lock:
            if not self._kids_active or self._kids_profile is None:
                return base
            profile = self._kids_profile
            remaining = max(30.0, self._kids_ends_at - time.monotonic())
        return replace(
            base,
            conversation_mode="pipeline",
            home_assistant_assist_enabled=False,
            kids_mode_enabled=True,
            kids_session_id=self._kids_session_id,
            kids_age_band=profile.age_band,
            kids_activity=profile.activity,
            language=profile.language,
            system_prompt=build_kids_prompt(profile),
            continuous_conversation=True,
            conversation_timeout_seconds=min(base.conversation_timeout_seconds, remaining),
            camera_enabled=False,
            camera_feed_enabled=False,
            face_tracking_enabled=False,
            doa_enabled=False,
            robot_tools_enabled=False,
            agent_tools_enabled=False,
            power_tools_enabled=False,
        )

    def _kids_session_is_current(self, session_id: str) -> bool:
        """Return whether network/audio work still belongs to the active child session."""
        with self._kids_lock:
            return bool(session_id) and self._kids_active and self._kids_session_id == session_id

    def queue_announcement(
        self,
        text: str,
        *,
        provider: str = "",
        model: str = "",
        voice: str = "",
        behavior: str = "wake_and_return",
        repeat: int = 1,
        pause_seconds: float = 1.0,
        contextual_offer_token: int = 0,
    ) -> dict[str, object]:
        """Queue TTS without exposing provider credentials to Reachy or the browser."""
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Announcement text is required")
        if len(clean_text) > 15_000:
            raise ValueError("Announcement text cannot exceed 15,000 characters")
        provider = provider.strip().lower()
        if provider not in {"", "configured", "elevenlabs"}:
            raise ValueError("Unsupported announcement TTS provider")
        behavior = behavior.strip().lower()
        if behavior not in _ANNOUNCEMENT_BEHAVIORS:
            raise ValueError("Unsupported announcement behavior")
        if isinstance(repeat, bool) or not 1 <= int(repeat) <= 10:
            raise ValueError("Announcement repeat must be between 1 and 10")
        if not 0.0 <= float(pause_seconds) <= 60.0:
            raise ValueError("Announcement pause must be between 0 and 60 seconds")
        if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
            raise RuntimeError("Announcements are blocked in Meeting and Sleep")
        if not self._audio_ready or self._announcement_worker is None:
            raise RuntimeError("Announcement audio is not ready")
        with self._announcement_state_lock:
            generation = self._announcement_cancellation_generation
            self._voice_activity_generation += 1
            item = Announcement(
                text=clean_text,
                provider=provider,
                model=model.strip(),
                voice=voice.strip(),
                behavior=behavior,
                repeat=int(repeat),
                pause_seconds=float(pause_seconds),
                cancellation_generation=generation,
                contextual_offer_token=contextual_offer_token,
            )
            try:
                self._announcement_queue.put_nowait(item)
            except queue.Full as exc:
                raise RuntimeError("Announcement queue is full") from exc
            queue_depth = self._announcement_queue.qsize()
        with self._status_lock:
            self._status.announcement_queue_depth = queue_depth
            self._status.announcement_last_error = ""
        _LOGGER.info("Queued announcement (%s characters, behavior=%s)", len(clean_text), behavior)
        return {"ok": True, "queued": True, "queue_depth": queue_depth}

    def stop_announcements(self, *, clear_queue: bool = True) -> dict[str, object]:
        """Stop announcement audio only; conversational and motor Stop controls remain separate."""
        active, cleared = self._cancel_announcements(clear_queue=clear_queue)
        return {"ok": True, "active_cancelled": active, "queued_cleared": cleared}

    def _cancel_announcements(self, *, clear_queue: bool) -> tuple[bool, int]:
        """Atomically invalidate the current item and optionally every queued item."""
        with self._announcement_state_lock:
            self._announcement_cancellation_generation += 1
            self._voice_activity_generation += 1
            current = self._announcement_current
            active = current is not None
            if current is not None:
                current.cancel_event.set()
                if current.contextual_offer_token:
                    self._contextual_offers.cancel(current.contextual_offer_token, "cancelled")
            cleared = self._clear_announcement_queue_unlocked() if clear_queue else 0
        if self._announcement_playing.is_set():
            try:
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
            except Exception:
                _LOGGER.debug("Could not stop announcement playback with silence asset", exc_info=True)
            self._clear_streamed_audio()
        return active, cleared

    def _clear_announcement_queue(self) -> int:
        with self._announcement_state_lock:
            return self._clear_announcement_queue_unlocked()

    def _clear_announcement_queue_unlocked(self) -> int:
        cleared = 0
        while True:
            try:
                item = self._announcement_queue.get_nowait()
                item.cancel_event.set()
                if item.contextual_offer_token:
                    self._contextual_offers.cancel(item.contextual_offer_token, "cancelled")
                self._announcement_queue.task_done()
                cleared += 1
            except queue.Empty:
                break
        with self._status_lock:
            self._status.announcement_queue_depth = 0
        return cleared

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
                    if self._camera_control_session_id:
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
        if self._kids_active or self._kids_locked:
            raise RuntimeError("Camera controls are blocked while Kids Mode is active or locked")
        if self._effective_power_mode() != "awake" or self._privacy_requested.is_set():
            raise RuntimeError("Camera controls require confirmed Awake outside privacy modes")
        if self._motors_enabled is not True:
            raise RuntimeError("Camera controls require confirmed Awake motor torque")
        if self._actions is None:
            raise RuntimeError("Robot action controller is not ready")

    def _validate_camera_control_session(self, session_id: str, sequence: int) -> None:
        now = time.monotonic()
        if not self._camera_control_session_id or session_id != self._camera_control_session_id:
            raise RuntimeError("Camera control session is not active")
        if now - self._camera_control_last_activity > 30.0:
            self._camera_control_session_id = ""
            raise RuntimeError("Camera control session expired")
        if sequence <= self._camera_control_last_sequence:
            raise RuntimeError("Camera control sequence is stale or replayed")
        self._camera_control_last_sequence = sequence
        self._camera_control_last_activity = now

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
                    if self._camera_control_session_id:
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

    def test_camera(self) -> dict[str, object]:
        """Capture one frame locally for setup diagnostics without returning it."""
        jpeg = self.camera_snapshot()
        return {"bytes": len(jpeg), "content_type": "image/jpeg"}

    def _assert_camera_allowed(self) -> None:
        with self._kids_lock:
            if self._kids_active:
                raise RuntimeError("Camera capture is blocked while Kids Mode is active")
        if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
            raise RuntimeError("Camera capture is blocked in the current privacy mode")

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
        if self._effective_power_mode() != "awake" or self._motors_enabled is not True:
            return False
        if self._privacy_requested.is_set():
            return False
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                return False
        with self._camera_control_lock:
            if self._camera_control_session_id:
                return False
        actions = self._actions
        return actions is not None and not actions.busy and actions.pending_count == 0

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

    def _before_robot_action(self) -> None:
        mode = self._effective_power_mode()
        if self._privacy_requested.is_set() or mode in {"meeting", "sleep"}:
            raise RuntimeError("Robot action was blocked by privacy mode")
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                raise RuntimeError("Robot action was blocked by Kids Mode")
        if self._motors_enabled is not True:
            raise RuntimeError("Robot action was blocked because motor torque is not confirmed")
        self._head_safely_folded = False
        self._set_face_tracking(False)
        if self._motion is not None:
            self._motion.suspend()

    def _after_robot_action(self) -> None:
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            return
        if self._motion is not None:
            self._motion.resume()
            if self._presence_action_active.is_set():
                return
            state = str(self.status().get("state") or "")
            if state == "speaking":
                self._motion.speaking()
            elif state in {"thinking", "transcribing", "synthesizing", "looking"}:
                self._motion.thinking()
            elif state == "listening":
                self._motion.listening()
            else:
                self._motion.idle()
        if self._face_tracking_desired and self._effective_power_mode() not in {"meeting", "sleep"}:
            self._set_face_tracking(True, weight=self._face_tracking_weight)

    def _on_robot_action_result(self, name: str, result: dict[str, object]) -> None:
        if name == "acknowledge_presence":
            self._presence_action_active.clear()
            succeeded = result.get("ok") is True
            reason = (
                "acknowledgement_cancelled"
                if result.get("error") == "Robot action was cancelled"
                else "motion_failed"
            )
            self._presence.complete_acknowledgement(succeeded=succeeded, reason=reason)
            if (
                not succeeded
                and self._motion is not None
                and self._effective_power_mode() == "awake"
                and self._motors_enabled is True
                and not self._privacy_requested.is_set()
            ):
                self._motion.resume()
            return
        with self._status_lock:
            self._status.last_robot_action = name
            if result.get("ok"):
                self._status.robot_actions += 1
                self._status.robot_action_last_error = ""
            else:
                self._status.robot_action_last_error = str(result.get("error") or "Robot action failed")

    def _set_status(self, state: str, detail: str = "", **updates: object) -> None:
        with self._status_lock:
            self._status.state = state
            self._status.detail = detail
            for key, value in updates.items():
                if hasattr(self._status, key):
                    setattr(self._status, key, value)

    def run(self) -> None:
        self._runtime_started = True
        self._control_ready.clear()
        self._set_status("starting", "Preparing the local wake-word model")
        config = self.config_loader()
        self._motion = VoiceMotion(self.robot, enabled=config.motion_enabled)
        self._actions = ReachyRobotActions(
            self.robot,
            self.stop_event,
            before_action=self._before_robot_action,
            after_action=self._after_robot_action,
            on_result=self._on_robot_action_result,
        )
        self._actions.start()
        model_directory = ensure_kws_model()
        self._spotter = HeyHermesSpotter(
            model_directory,
            self.assets / "keywords.txt",
            score=config.wake_keyword_score,
            threshold=config.wake_keyword_threshold,
        )
        self._set_status("starting", "Starting Reachy audio", model_ready=True)

        self.robot.media.start_recording()
        self._recording = True
        self.robot.media.start_playing()
        time.sleep(0.8)
        detected_rate = int(self.robot.media.get_input_audio_samplerate())
        if detected_rate <= 0:
            raise RuntimeError("Reachy audio input did not report a valid sample rate")
        self._sample_rate = detected_rate
        output_rate = int(self.robot.media.get_output_audio_samplerate())
        if output_rate > 0:
            self._output_sample_rate = output_rate
        self._audio_ready = True
        self._announcement_worker = threading.Thread(
            target=self._run_announcement_worker,
            name="reachy-hermes-announcements",
            daemon=True,
        )
        self._announcement_worker.start()
        _LOGGER.info(
            "Reachy Hermes audio ready: input=%s Hz output=%s Hz",
            self._sample_rate,
            self._output_sample_rate,
        )
        self._head_safely_folded = self._read_head_safely_folded()
        self._apply_power_mode()
        self._control_ready.set()
        self._gesture_stop_requested.clear()
        self._gesture_worker = threading.Thread(
            target=self._run_gesture_worker,
            name="reachy-hermes-gestures",
            daemon=True,
        )
        self._gesture_worker.start()
        if config.home_assistant_enabled:
            try:
                provider = HermesHomeAssistantProvider(self, config_loader=self.config_loader)
                self._home_assistant_bridge = HomeAssistantBridge(provider, config=config)
                self._home_assistant_bridge.start()
                self._home_assistant_error = ""
                _LOGGER.info(
                    "Home Assistant ESPHome bridge ready as %s on port %s",
                    self._home_assistant_bridge.identity.name,
                    config.home_assistant_port,
                )
            except Exception as exc:
                self._home_assistant_error = str(exc)
                self._home_assistant_bridge = None
                _LOGGER.exception("Home Assistant bridge did not start; Hermes voice remains available")

        try:
            self._listen_for_wake_word()
        finally:
            bridge, self._home_assistant_bridge = self._home_assistant_bridge, None
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    _LOGGER.exception("Home Assistant bridge did not close cleanly")
            self._control_ready.clear()
            self._set_status("stopping")
            with self._kids_lock:
                kids_timer, self._kids_timer = self._kids_timer, None
                kids_warning_timer, self._kids_warning_timer = self._kids_warning_timer, None
                self._kids_active = False
            if kids_timer is not None:
                kids_timer.cancel()
            if kids_warning_timer is not None:
                kids_warning_timer.cancel()
            self._audio_ready = False
            self._cancel_announcements(clear_queue=True)
            self._gesture_stop_requested.set()
            gesture_worker = self._gesture_worker
            if gesture_worker is not None and gesture_worker.is_alive():
                gesture_worker.join(timeout=5.0)
                if gesture_worker.is_alive():
                    _LOGGER.warning("Gesture worker did not exit before camera teardown")
            worker = self._announcement_worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=5.0)
                if worker.is_alive():
                    _LOGGER.warning("Announcement worker did not exit before media teardown")
            with self._status_lock:
                self._status.announcement_current_preview = ""
                self._status.announcement_last_text = ""
            self._face_tracking_desired = False
            self._set_face_tracking(False)
            if self._actions is not None:
                self._actions.close()
            try:
                if self._recording:
                    self.robot.media.stop_recording()
                    self._recording = False
            except Exception:
                _LOGGER.debug("Audio recording was already stopped", exc_info=True)
            try:
                self.robot.media.stop_playing()
            except Exception:
                _LOGGER.debug("Audio playback was already stopped", exc_info=True)
            if self._motion is not None:
                try:
                    self._motion.close()
                except Exception:
                    _LOGGER.exception("Could not disable voice wobbling after audio teardown")

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

    def _listen_for_wake_word(self) -> None:
        assert self._spotter is not None
        while not self.stop_event.is_set():
            try:
                config = self._kids_voice_config(self.config_loader())
            except Exception as exc:
                self._set_status("configuration_error", str(exc), last_error=str(exc))
                self.stop_event.wait(1.0)
                continue

            mode = self._effective_power_mode()
            with self._status_lock:
                applied_mode = self._status.power_mode
            if mode != applied_mode:
                self._apply_power_mode()
            if mode in {"meeting", "sleep"}:
                remaining = 0
                if mode == "meeting":
                    with self._power_lock:
                        remaining = max(0, int(self._meeting_until - time.monotonic()))
                with self._status_lock:
                    current_state = self._status.state
                    self._status.meeting_seconds_remaining = remaining
                if current_state != mode:
                    self._set_status(
                        mode,
                        "Voice is disabled in Meeting" if mode == "meeting" else "Voice is disabled in Sleep",
                        power_mode=mode,
                        meeting_seconds_remaining=remaining,
                    )
                self.stop_event.wait(0.25)
                continue
            if self._announcement_active.is_set():
                self.stop_event.wait(0.05)
                continue
            if self._presentation_window_active():
                self.stop_event.wait(0.05)
                continue

            if not config.configured and not config.home_assistant_assist_enabled:
                self._set_status("waiting_for_configuration", "Open the app settings and configure the Hermes bridge")
                self.stop_event.wait(1.0)
                continue

            detail = "Local wake detection only" if mode == "standby" else _WAKE_PROMPT
            self._set_status("waiting_for_wake_word", detail)
            frame = self._read_16k_frame()
            if frame is None:
                continue
            if config.doa_enabled:
                try:
                    self._sample_doa()
                except Exception:
                    _LOGGER.debug("Could not sample local wake DOA", exc_info=True)
            rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
            peak = float(np.max(np.abs(frame))) if frame.size else 0.0
            with self._status_lock:
                self._status.audio_rms = rms
                self._status.audio_peak = peak
                self._status.audio_frames_processed += 1
            self._noise.update(frame)
            keyword = self._spotter.accept(frame, 16000)
            if not keyword:
                continue
            if self._presentation_window_active():
                self._spotter.reset()
                continue
            now = time.monotonic()
            if now - self._last_wake_at < config.wake_cooldown_seconds:
                continue
            self._last_wake_at = now
            _LOGGER.info("Wake word detected: %s", keyword)
            with self._announcement_state_lock:
                wake_activity_generation = self._voice_activity_generation
                announcement_active = self._announcement_current is not None
            if announcement_active:
                _LOGGER.debug("Ignoring wake word while an announcement owns the voice channel")
                continue
            with self._motor_transition_lock:
                if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                    continue
                self._accept_wake_turn()
                try:
                    self._set_motor_mode(True, wake=True)
                except RuntimeError as exc:
                    self._set_status("power_transition_error", str(exc), last_error=str(exc))
                    self._signal_error()
                    continue
                if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                    self._fold_head_before_torque_release()
                    continue
            try:
                with self._voice_activity_lock:
                    with self._announcement_state_lock:
                        stale_wake = (
                            self._voice_activity_generation != wake_activity_generation
                            or self._announcement_current is not None
                        )
                    if (
                        stale_wake
                        or self.stop_event.is_set()
                        or self._privacy_requested.is_set()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                        or self._motors_enabled is not True
                    ):
                        _LOGGER.info("Discarding stale wake after announcement/power arbitration")
                        continue
                    self.cancel_agent_work("session_changed")
                    self._orient_to_voice(config)
                    self.observe_presence(
                        PresenceObservation(source="voice", occupied=True, attentive=True),
                        allow_acknowledgement=False,
                    )
                    self._face_tracking_desired = config.face_tracking_enabled
                    self._face_tracking_weight = config.face_tracking_weight
                    if self._face_tracking_desired:
                        self._set_face_tracking(True, weight=self._face_tracking_weight)
                    self._run_selected_voice_conversation(config, keyword)
            except Exception as exc:
                _LOGGER.exception("Reachy Hermes voice turn failed")
                self._set_status("error", str(exc), last_error=str(exc))
                self._signal_error()
                self.stop_event.wait(0.4)
            finally:
                self._face_tracking_desired = False
                self._set_face_tracking(False)
                self._spotter.reset()
                if self._motion is not None:
                    self._motion.idle()
                if self._effective_power_mode() == "standby":
                    self._fold_head_before_torque_release()

    def _run_selected_voice_conversation(self, config: AppConfig, keyword: str) -> None:
        """Route locked child sessions before every adult conversation provider."""
        if config.kids_mode_enabled:
            self._run_conversation(config)
        elif config.home_assistant_assist_enabled:
            try:
                self._run_home_assistant_conversation(config, keyword)
            finally:
                self._stop_home_assistant_voice_if_active()
        elif config.conversation_mode == "realtime":
            self._run_realtime_conversation(config)
        else:
            self._run_conversation(config)

    def _handle_power_mode_call(
        self,
        session: RealtimeBridgeSession,
        power_call: PowerModeToolCall,
    ) -> dict[str, object]:
        """Apply a completed local power call and report its real resulting state."""
        mode = power_call.mode
        duration_minutes = power_call.duration_minutes
        if mode not in _POWER_MODES:
            result: dict[str, object] = {
                "ok": False,
                "error": "Mode must be standby, awake, meeting, or sleep",
            }
        elif mode == "meeting" and (duration_minutes is None or not 1 <= duration_minutes <= 480):
            result = {
                "ok": False,
                "error": "Meeting duration must be between 1 and 480 minutes",
            }
        else:
            duration_seconds = float((duration_minutes or 30) * 60) if mode == "meeting" else 0.0
            try:
                self.set_power_mode(mode, duration_seconds=duration_seconds)
                result = {"ok": True, "mode": mode}
            except RuntimeError as exc:
                result = {"ok": False, "mode": mode, "error": str(exc)}
            if mode == "meeting":
                result["duration_minutes"] = duration_minutes or 30
        session.send_tool_result(
            power_call.call_id,
            result,
            continue_response=not result.get("ok") or mode == "awake",
        )
        _LOGGER.info("Realtime power mode tool: %s", result)
        return result

    def _home_assistant_provider(self) -> HermesHomeAssistantProvider:
        bridge = self._home_assistant_bridge
        if bridge is None or not bridge.connected:
            raise RuntimeError("Home Assistant is not connected to the Reachy ESPHome bridge")
        provider = bridge.provider
        if not isinstance(provider, HermesHomeAssistantProvider):
            raise RuntimeError("Home Assistant runtime provider is unavailable")
        return provider

    def _stop_home_assistant_voice_if_active(self) -> None:
        bridge = self._home_assistant_bridge
        if bridge is None:
            return
        provider = bridge.provider
        if not isinstance(provider, HermesHomeAssistantProvider):
            return
        if not bool(provider.voice_snapshot().get("active")):
            return
        bridge.stop_voice()
        provider.cancel_voice()

    def _play_home_assistant_media(self, provider: HermesHomeAssistantProvider, url: str) -> None:
        """Download and play one HA-owned audio URL with peer and size checks."""
        validated = provider.validate_media_url(url)
        parsed = httpx.URL(validated)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}:
            suffix = ".audio"
        temporary_path = ""
        maximum_bytes = 15 * 1024 * 1024
        try:
            with tempfile.NamedTemporaryFile(prefix="reachy-ha-", suffix=suffix, delete=False) as temporary:
                temporary_path = temporary.name
                total = 0
                with httpx.stream("GET", validated, timeout=20.0, follow_redirects=True) as response:
                    response.raise_for_status()
                    provider.validate_media_url(str(response.url))
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > maximum_bytes:
                        raise RuntimeError("Home Assistant media exceeds the 15 MB limit")
                    for chunk in response.iter_bytes():
                        if self.stop_event.is_set() or self._privacy_requested.is_set():
                            raise RuntimeError("Home Assistant media playback was cancelled")
                        total += len(chunk)
                        if total > maximum_bytes:
                            raise RuntimeError("Home Assistant media exceeds the 15 MB limit")
                        temporary.write(chunk)
                if total == 0:
                    raise RuntimeError("Home Assistant returned empty media")

            duration = 0.0
            try:
                from mutagen import File as MutagenFile

                metadata = MutagenFile(temporary_path)
                duration = float(metadata.info.length) if metadata is not None and metadata.info is not None else 0.0
            except Exception:
                duration = 0.0
            if not math.isfinite(duration) or duration <= 0:
                duration = min(60.0, max(1.0, total / 32_000.0))
            if duration > 120.0:
                raise RuntimeError("Home Assistant media exceeds the 120 second playback limit")

            if self._motion is not None:
                self._motion.speaking()
            self._set_status("speaking", "Home Assistant is responding")
            self.robot.media.play_sound(temporary_path)
            deadline = time.monotonic() + duration + 0.25
            while time.monotonic() < deadline:
                if (
                    self.stop_event.is_set()
                    or self._conversation_stop_requested.is_set()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    try:
                        self.robot.media.play_sound(str(self.assets / "silence.wav"))
                    except Exception:
                        pass
                    raise RuntimeError("Home Assistant media playback was cancelled")
                self.stop_event.wait(min(0.05, deadline - time.monotonic()))
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def queue_home_assistant_media(self, url: str | list[str], *, announcement: bool) -> None:
        """Play an ESPHome media-player request without blocking the protocol loop."""
        provider = self._home_assistant_provider()
        urls = [url] if isinstance(url, str) else list(url)
        if not urls:
            raise ValueError("Home Assistant media playlist is empty")
        for item in urls:
            provider.validate_media_url(item)

        def play() -> None:
            try:
                with self._voice_activity_lock:
                    for item in urls:
                        self._play_home_assistant_media(provider, item)
                if announcement and self._home_assistant_bridge is not None:
                    self._home_assistant_bridge.voice_announcement_finished()
            except Exception as exc:
                _LOGGER.warning("Home Assistant media playback failed: %s", exc)
                with self._status_lock:
                    self._status.last_error = str(exc)
            finally:
                if self._motion is not None:
                    self._motion.idle()

        threading.Thread(target=play, name="reachy-ha-media", daemon=True).start()

    def _run_home_assistant_conversation(self, config: AppConfig, wake_word: str) -> None:
        """Stream a locally awakened voice turn through Home Assistant Assist."""
        bridge = self._home_assistant_bridge
        provider = self._home_assistant_provider()
        assert bridge is not None
        conversation_id = str(uuid.uuid4())
        overall_deadline = time.monotonic() + config.conversation_timeout_seconds
        wake_phrase = wake_word or "Hey Hermes"
        follow_up = False

        while not self.stop_event.is_set() and time.monotonic() < overall_deadline:
            provider.begin_voice()
            if not bridge.start_voice(
                wake_word_phrase="" if follow_up else wake_phrase,
                conversation_id=conversation_id,
            ):
                raise RuntimeError("Home Assistant Assist request could not be sent")
            pipeline_deadline = min(overall_deadline, time.monotonic() + 120.0)
            latest: dict[str, object] = {}
            last_stage = ""
            while not self.stop_event.is_set() and time.monotonic() < pipeline_deadline:
                if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                    raise RuntimeError("Home Assistant Assist was cancelled by privacy mode")
                latest = provider.wait_voice_update(0.01)
                error = str(latest.get("error") or "")
                if error:
                    raise RuntimeError(error)
                stage = str(latest.get("stage") or "")
                if stage != last_stage and stage == "listening":
                    self._set_status("listening", "Home Assistant Assist is listening")
                    if self._motion is not None:
                        self._motion.listening()
                elif stage != last_stage and stage == "thinking":
                    self._set_status("thinking", "Home Assistant Assist is processing")
                    if self._motion is not None:
                        self._motion.thinking()
                last_stage = stage

                tts_url = str(latest.get("tts_url") or "")
                if tts_url:
                    self._play_home_assistant_media(provider, tts_url)
                    bridge.voice_announcement_finished()
                    provider.voice_playback_finished()
                    latest = provider.voice_snapshot()

                if bool(latest.get("done")):
                    break
                if bool(latest.get("streaming")):
                    frame = self._read_16k_frame()
                    if frame is not None:
                        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()
                        if not bridge.send_voice_audio(pcm16):
                            raise RuntimeError("Home Assistant disconnected during Assist audio streaming")
            else:
                raise RuntimeError("Home Assistant Assist pipeline timed out")

            with self._status_lock:
                self._status.turns_completed += 1
            follow_up = bool(latest.get("continue_conversation")) or config.continuous_conversation
            if not follow_up:
                return
            self._set_status("listening", "Home Assistant Assist is waiting for a follow-up")
        if time.monotonic() >= overall_deadline:
            raise RuntimeError("Home Assistant Assist conversation timed out")

    def _run_realtime_conversation(self, config: AppConfig) -> None:
        """Run a persistent speech-to-speech session after the local wake word."""
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            return
        broker_context = self.agent_broker_context(explicit_private_intent=False)
        agent_request_id = ""
        if broker_context.capability_profile == "agent":
            agent_request_id, broker_context = self._begin_agent_request("Realtime Agent session")
        session = RealtimeBridgeSession(
            config,
            agent_context=broker_context,
            agent_request_id=agent_request_id,
        )
        transcript_parts: list[str] = []
        response_parts: list[str] = []
        last_activity = time.monotonic()
        speaking = False
        generation_done = False
        playback = RealtimePlayback()
        handled_camera_call_ids: set[str] = set()
        handled_robot_call_ids: set[str] = set()
        handled_power_call_ids: set[str] = set()
        active_response_id = ""
        interrupted_response_ids: set[str] = set()
        self._play_asset("listening.wav")
        self._discard_audio(0.34)
        self._set_status(
            "connecting_realtime",
            "Opening private GPT Realtime session",
            bridge_healthy=True,
            last_error="",
        )
        try:
            session.start()
        except Exception:
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=False)
            raise
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            session.close()
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=False)
            return
        self._set_status("listening", "Realtime session active")
        if self._motion is not None:
            self._motion.listening()
        try:
            while not self.stop_event.is_set() and not self._turn_stop_requested():
                if self._effective_power_mode() in {"meeting", "sleep"}:
                    break
                if time.monotonic() - last_activity >= config.conversation_timeout_seconds:
                    _LOGGER.info("Realtime conversation closed after inactivity timeout")
                    break

                frame = self._read_16k_frame()
                if frame is not None:
                    session.send_audio(resample_linear(frame, 16000, 24000))

                for event in session.events():
                    kind = event.type
                    payload = event.payload
                    event_response_id = realtime_response_id(kind, payload)
                    audio_item_id = realtime_audio_item_id(kind, payload)
                    if audio_item_id:
                        playback.item_id = audio_item_id
                    if kind in {"bridge.error", "error"}:
                        error = payload.get("error")
                        if isinstance(error, dict):
                            error = error.get("message") or error
                        message = str(error or "Realtime session failed")
                        if "Only model output audio messages can be truncated" in message:
                            _LOGGER.warning(
                                "Realtime audio truncation was rejected after local queue clear: %s",
                                message,
                            )
                            continue
                        raise RealtimeBridgeError(message)
                    if kind != "input_audio_buffer.speech_started" and (
                        event_response_id and event_response_id in interrupted_response_ids
                    ):
                        if kind in {"response.done", "response.cancelled", "response.failed"}:
                            interrupted_response_ids.discard(event_response_id)
                            if event_response_id == active_response_id:
                                active_response_id = ""
                        continue
                    if kind == "input_audio_buffer.speech_started":
                        now = time.monotonic()
                        last_activity = now
                        transcript_parts.clear()
                        if active_response_id:
                            interrupted_response_ids.add(active_response_id)
                        if speaking or playback.audible(now):
                            played_ms = playback.played_ms(now)
                            self._clear_streamed_audio()
                            if playback.item_id:
                                session.truncate_audio(playback.item_id, played_ms)
                            _LOGGER.info(
                                "Realtime interruption: cleared buffered audio at %s ms",
                                played_ms,
                            )
                            speaking = False
                            generation_done = False
                            playback.reset()
                            with self._status_lock:
                                self._status.interruptions += 1
                        self._set_status("listening", "Listening to interruption")
                        if self._motion is not None:
                            self._motion.listening()
                    elif kind in {
                        "conversation.item.input_audio_transcription.delta",
                        "conversation.item.input_audio_transcription.completed",
                    }:
                        text = str(payload.get("delta") or payload.get("transcript") or "")
                        if text:
                            if kind.endswith(".delta"):
                                transcript_parts.append(text)
                            else:
                                transcript_parts = [text]
                            self._set_status(
                                "thinking",
                                "Hermes is responding",
                                transcript="".join(transcript_parts).strip(),
                                stt_provider="openai-realtime",
                            )
                    camera_call_id = completed_camera_call_id(kind, payload)
                    robot_call = completed_robot_tool_call(kind, payload)
                    power_call = completed_power_mode_call(kind, payload)
                    if power_call is not None and power_call.call_id not in handled_power_call_ids:
                        handled_power_call_ids.add(power_call.call_id)
                        if config.power_tools_enabled:
                            self._handle_power_mode_call(session, power_call)
                        else:
                            session.send_tool_result(
                                power_call.call_id,
                                {"ok": False, "error": "Power tools are disabled for this session"},
                            )
                    elif camera_call_id and camera_call_id not in handled_camera_call_ids:
                        handled_camera_call_ids.add(camera_call_id)
                        self._set_status("looking", "Capturing one on-demand camera frame")
                        try:
                            if not config.camera_enabled:
                                raise RuntimeError("Camera access is disabled in Reachy settings")
                            if self._effective_power_mode() in {"meeting", "sleep"}:
                                raise RuntimeError("Camera capture is blocked in the current privacy mode")
                            jpeg = self._capture_camera_jpeg()
                            session.send_camera_frame(camera_call_id, jpeg)
                            with self._status_lock:
                                self._status.camera_captures += 1
                                self._status.camera_last_error = ""
                            _LOGGER.info("Sent on-demand Reachy camera frame: %s bytes", len(jpeg))
                            self._set_status("thinking", "Hermes is looking at the fresh camera frame")
                        except Exception as exc:
                            message = str(exc)
                            _LOGGER.exception("Could not provide Reachy camera frame")
                            with self._status_lock:
                                self._status.camera_last_error = message
                            session.send_camera_error(camera_call_id, message)
                            self._set_status("thinking", "Camera capture failed; Hermes is responding")
                    elif robot_call is not None and robot_call.call_id not in handled_robot_call_ids:
                        handled_robot_call_ids.add(robot_call.call_id)
                        if not config.robot_tools_enabled:
                            result: dict[str, object] = {
                                "ok": False,
                                "error": "Robot tools are disabled in Reachy settings",
                            }
                        elif self._effective_power_mode() in {"meeting", "sleep"}:
                            result = {
                                "ok": False,
                                "error": "Physical actions are blocked in the current privacy mode",
                            }
                        elif self._actions is None:
                            result = {"ok": False, "error": "Robot action controller is unavailable"}
                        else:

                            def complete_robot_tool(
                                completed: dict[str, object],
                                call_id: str = robot_call.call_id,
                            ) -> None:
                                try:
                                    session.send_tool_result(call_id, completed)
                                except Exception:
                                    _LOGGER.exception("Could not complete Realtime robot tool %s", call_id)

                            result = self._actions.enqueue(
                                robot_call.name,
                                robot_call.arguments,
                                on_complete=complete_robot_tool,
                            )
                        if not result.get("accepted"):
                            session.send_tool_result(robot_call.call_id, result)
                        _LOGGER.info("Realtime robot tool %s: %s", robot_call.name, result)
                        self._set_status("thinking", "Hermes queued a Reachy action")
                    elif kind == "response.created":
                        active_response_id = event_response_id
                        last_activity = time.monotonic()
                        generation_done = False
                        playback.reset()
                        self._set_status("thinking", "Hermes is responding")
                        if self._motion is not None:
                            self._motion.thinking()
                    elif kind == "response.output_item.added":
                        last_activity = time.monotonic()
                        self._set_status("thinking", "Hermes is responding")
                    elif kind in {"response.output_audio.delta", "response.audio.delta"}:
                        audio = session.audio_samples(event)
                        if audio.size:
                            now = time.monotonic()
                            if not speaking:
                                speaking = True
                                response_parts.clear()
                                self._set_status(
                                    "speaking",
                                    "Hermes Realtime is speaking",
                                    tts_provider="openai-realtime",
                                )
                                if self._motion is not None:
                                    self._motion.speaking()
                            output = resample_linear(audio, 24000, self._output_sample_rate)
                            self.robot.media.push_audio_sample(output)
                            playback.add(now, output.size / self._output_sample_rate)
                            last_activity = now
                    elif kind in {
                        "response.output_audio_transcript.delta",
                        "response.audio_transcript.delta",
                    }:
                        response_parts.append(str(payload.get("delta") or ""))
                        self._set_status(
                            "speaking",
                            "Hermes Realtime is speaking",
                            response_preview="".join(response_parts)[-240:],
                        )
                    elif kind in {"response.done", "response.output_audio.done", "response.audio.done"}:
                        if kind == "response.done":
                            if not event_response_id or event_response_id == active_response_id:
                                active_response_id = ""
                            with self._status_lock:
                                self._status.turns_completed += 1
                            generation_done = True
                        last_activity = time.monotonic()
                    if self._turn_stop_requested():
                        break
                if self._turn_stop_requested():
                    break
                if generation_done and speaking and not playback.audible(time.monotonic()):
                    speaking = False
                    generation_done = False
                    playback.reset()
                    self._set_status("listening", "Waiting for a follow-up")
                    if self._motion is not None:
                        self._motion.listening()
        finally:
            session.close()
            self._clear_streamed_audio()
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=True)

    def _clear_streamed_audio(self) -> None:
        """Flush Realtime appsrc output without stopping microphone capture."""
        audio = getattr(self.robot.media, "audio", None)
        clear = getattr(audio, "clear_player", None)
        if callable(clear):
            clear()

    def _announcement_item_cancelled(self, item: Announcement) -> bool:
        with self._announcement_state_lock:
            return (
                self.stop_event.is_set()
                or item.cancel_event.is_set()
                or item.cancellation_generation != self._announcement_cancellation_generation
                or self._announcement_current is not item
            )

    def _capture_contextual_offer_response(
        self,
        item: Announcement,
        client: HermesBridgeClient,
        config: AppConfig,
    ) -> None:
        """Listen once for an explicit yes/no after a successfully spoken offer."""
        token = item.contextual_offer_token
        if not token or not self._contextual_offers.mark_spoken(token):
            return
        if not self._contextual_offers.begin_listening(token):
            return
        if self._announcement_item_cancelled(item):
            self._contextual_offers.cancel(token, "cancelled")
            return
        self._set_status("listening", "Waiting for a yes or no response")
        self._play_asset("listening.wav")
        self._discard_audio(0.34)
        response_config = replace(
            config,
            initial_speech_timeout_seconds=config.contextual_offer_response_window_seconds,
            max_utterance_seconds=4.0,
            end_silence_seconds=min(config.end_silence_seconds, 0.6),
        )
        endpoint = self._record_utterance(
            response_config,
            should_stop=lambda: self._announcement_item_cancelled(item)
            or self._privacy_requested.is_set()
            or self._effective_power_mode() != "awake",
        )
        if (
            not endpoint.speech_detected
            or endpoint.samples.size == 0
        ):
            self._contextual_offers.finish_listening(token)
            return
        if (
            self._announcement_item_cancelled(item)
            or self._privacy_requested.is_set()
            or self._effective_power_mode() != "awake"
        ):
            self._contextual_offers.cancel(token, "cancelled")
            return
        transcript = client.transcribe(encode_wav(endpoint.samples, 16000))
        response = parse_offer_response(transcript)
        if response == "unknown":
            self._contextual_offers.cancel(token, "unrecognized_response")
            return
        try:
            self.respond_to_contextual_offer(token, response)
        except RuntimeError:
            # The trusted phone may have answered while local transcription ran.
            _LOGGER.info("Contextual offer response was already resolved")

    def _run_announcement_worker(self) -> None:
        """Serialize announcements with conversations and restore the requested physical state."""
        while not self.stop_event.is_set():
            try:
                item = self._announcement_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._announcement_state_lock:
                valid = (
                    not self.stop_event.is_set()
                    and not item.cancel_event.is_set()
                    and item.cancellation_generation == self._announcement_cancellation_generation
                )
                if valid:
                    self._announcement_current = item
                    self._announcement_active.set()
            if not valid:
                self._announcement_queue.task_done()
                continue
            with self._status_lock:
                self._status.announcement_busy = True
                self._status.announcement_queue_depth = self._announcement_queue.qsize()
                self._status.announcement_current_preview = item.text[:240]
                self._status.announcement_last_error = ""
            woke_for_announcement = False
            client: HermesBridgeClient | None = None
            try:
                with self._voice_activity_lock:
                    if self._announcement_item_cancelled(item):
                        continue
                    mode = self._effective_power_mode()
                    if mode in {"meeting", "sleep"} or self._privacy_requested.is_set():
                        raise RuntimeError("Announcement was blocked by the current privacy mode")
                    config = self.config_loader()
                    if item.contextual_offer_token:
                        if not self._contextual_offers.is_queued(item.contextual_offer_token):
                            continue
                        reason = self._presence_suppression_reason(
                            config,
                            owns_voice_activity=True,
                            owns_announcement=True,
                        )
                        with self._agent_lock:
                            agent_active = self._capability_profile == "agent"
                        if reason or not config.contextual_offers_enabled or not agent_active:
                            blocked_reason = reason or (
                                "contextual_offers_disabled"
                                if not config.contextual_offers_enabled
                                else "agent_profile_inactive"
                            )
                            self._contextual_offers.cancel(
                                item.contextual_offer_token,
                                blocked_reason,
                            )
                            continue
                    if not config.configured:
                        raise RuntimeError("Configure the Hermes bridge before making announcements")
                    if item.behavior in {"wake_and_return", "wake_and_stay"} and mode == "standby":
                        self.set_power_mode("awake")
                        woke_for_announcement = True
                    if self._announcement_item_cancelled(item):
                        continue
                    speech_config = replace(
                        config,
                        tts_provider=item.provider or config.tts_provider,
                        tts_model=item.model or config.tts_model,
                        tts_voice=item.voice or config.tts_voice,
                    )
                    client = HermesBridgeClient(speech_config)
                    self._set_status(
                        "announcement_synthesizing",
                        "Generating announcement speech",
                        announcement_current_preview=item.text[:240],
                    )
                    speech = client.synthesize(item.text)
                    for index in range(item.repeat):
                        if self._announcement_item_cancelled(item):
                            break
                        self._set_status(
                            "announcing",
                            f"Playing announcement {index + 1} of {item.repeat}",
                            announcement_provider=speech.provider,
                        )
                        self._play_announcement_audio(item, speech, item.text)
                        if index + 1 < item.repeat and item.cancel_event.wait(item.pause_seconds):
                            break
                    if not self._announcement_item_cancelled(item):
                        with self._status_lock:
                            self._status.announcements_completed += 1
                            self._status.announcement_last_text = item.text[:240]
                        if item.contextual_offer_token:
                            self._capture_contextual_offer_response(item, client, config)
            except Exception as exc:
                _LOGGER.exception("Announcement failed")
                if item.contextual_offer_token:
                    self._contextual_offers.cancel(item.contextual_offer_token, "speech_failed")
                with self._status_lock:
                    self._status.announcement_last_error = str(exc)
            finally:
                if client is not None:
                    client.close()
                with self._motor_transition_lock:
                    with self._announcement_state_lock:
                        owns_transition = (
                            self._announcement_current is item
                            and item.cancellation_generation == self._announcement_cancellation_generation
                            and not item.cancel_event.is_set()
                        )
                    if (
                        owns_transition
                        and woke_for_announcement
                        and item.behavior == "wake_and_return"
                        and not self.stop_event.is_set()
                        and self._effective_power_mode() == "awake"
                        and not self._privacy_requested.is_set()
                    ):
                        try:
                            self.set_power_mode("standby", cancel_announcements=False)
                        except RuntimeError as exc:
                            with self._status_lock:
                                self._status.announcement_last_error = str(exc)
                with self._announcement_state_lock:
                    if self._announcement_current is item:
                        self._announcement_current = None
                    self._announcement_active.clear()
                self._announcement_queue.task_done()
                with self._status_lock:
                    self._status.announcement_busy = False
                    self._status.announcement_queue_depth = self._announcement_queue.qsize()
                    self._status.announcement_current_preview = ""
                if (
                    item.contextual_offer_token
                    and not self.stop_event.is_set()
                    and not self._privacy_requested.is_set()
                    and self._effective_power_mode() == "awake"
                ):
                    self._set_status("waiting_for_wake_word", _WAKE_PROMPT)

    def _play_announcement_audio(self, item: Announcement, speech: SpeechAudio, text: str) -> None:
        suffix = speech.extension if speech.extension.startswith(".") else ".audio"
        with tempfile.NamedTemporaryFile(prefix="reachy-announcement-", suffix=suffix, delete=False) as output:
            output.write(speech.data)
            path = Path(output.name)
        try:
            if self._announcement_item_cancelled(item):
                return
            duration = self._announcement_audio_duration(path, fallback_text=text)
            if self._motion is not None and self._motors_enabled is True:
                self._motion.speaking()
            self._announcement_playing.set()
            self.robot.media.play_sound(str(path))
            deadline = time.monotonic() + duration + 0.25
            while time.monotonic() < deadline and not self.stop_event.is_set():
                if item.cancel_event.wait(0.02) or self._announcement_item_cancelled(item):
                    self.robot.media.play_sound(str(self.assets / "silence.wav"))
                    self._clear_streamed_audio()
                    break
                if self._effective_power_mode() in {"meeting", "sleep"}:
                    break
            else:
                # Reachy's player is asynchronous. Explicitly flush at the measured
                # end so ownership cannot be released while old audio remains queued.
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
                self._clear_streamed_audio()
        finally:
            self._announcement_playing.clear()
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    def _announcement_audio_duration(path: Path, *, fallback_text: str) -> float:
        """Return full announcement duration, bounded only by a hard 30-minute safety ceiling."""
        try:
            from mutagen import File as MutagenFile

            media = MutagenFile(path)
            if media is not None and media.info is not None:
                return max(0.1, min(float(media.info.length), 1800.0))
        except Exception:
            _LOGGER.debug("Could not inspect announcement TTS duration", exc_info=True)
        return max(1.0, min(len(fallback_text) / 13.0 + 0.6, 1800.0))

    def _run_conversation(self, initial_config: AppConfig) -> None:
        config = initial_config
        client = HermesBridgeClient(config)

        def conversation_is_current() -> bool:
            return not initial_config.kids_mode_enabled or self._kids_session_is_current(
                initial_config.kids_session_id
            )

        try:
            health = client.health()
            self._set_status("listening", "Wake word accepted", bridge_healthy=True, last_error="")
            _LOGGER.debug("Hermes bridge health: %s", health.get("status"))

            first_turn = True
            while (
                not self.stop_event.is_set()
                and not self._turn_stop_requested()
                and conversation_is_current()
            ):
                if self._effective_power_mode() in {"meeting", "sleep"}:
                    break
                if not first_turn:
                    config = initial_config if initial_config.kids_mode_enabled else self.config_loader()
                    if not config.continuous_conversation:
                        break
                    self._set_status("listening", "Waiting for a follow-up")
                first_turn = False

                self._play_asset("listening.wav")
                if self._motion is not None:
                    self._motion.listening()
                # Keep draining the microphone while the local earcon plays so
                # its samples cannot become the start of the user's utterance.
                self._discard_audio(0.34)

                endpoint = self._record_utterance(config)
                if not endpoint.speech_detected or endpoint.samples.size == 0:
                    self._set_status("waiting_for_wake_word", "No speech detected")
                    if not config.continuous_conversation:
                        self._signal_error()
                    break

                self._play_asset("processing.wav")
                if self._motion is not None:
                    self._motion.thinking()
                self._set_status("transcribing", "Command received; transcribing")
                transcript = client.transcribe(encode_wav(endpoint.samples, 16000))
                if (
                    not conversation_is_current()
                    or self.stop_event.is_set()
                    or self._turn_stop_requested()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                _LOGGER.info("Transcript accepted (%s characters)", len(transcript))
                self._set_status(
                    "thinking",
                    "Hermes is working",
                    transcript=transcript,
                    stt_provider=client.last_stt_provider,
                )

                with self._agent_lock:
                    agent_profile_active = self._capability_profile == "agent"
                if agent_profile_active:
                    request_id, broker_context = self._begin_agent_request(transcript)
                    try:
                        response_text = client.ask_agent(
                            transcript,
                            broker_context,
                            request_id=request_id,
                        )
                    except Exception:
                        self._finish_agent_request(
                            request_id,
                            broker_context.session_generation,
                            succeeded=False,
                        )
                        raise
                    if not self._finish_agent_request(
                        request_id,
                        broker_context.session_generation,
                        succeeded=True,
                    ):
                        break
                else:
                    response_text = client.chat(transcript)
                if (
                    not conversation_is_current()
                    or self.stop_event.is_set()
                    or self._turn_stop_requested()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                preserve_ispy_guess_motion = False
                if (
                    client.config.kids_mode_enabled
                    and client.config.kids_activity == "ispy"
                    and client.last_kids_ispy_role == "player_picker"
                    and client.last_kids_ispy_phase == "awaiting_confirmation"
                ):
                    with self._kids_lock:
                        ispy_generation = self._kids_generation
                    preserve_ispy_guess_motion = self._try_ispy_player_guess_motion(ispy_generation)
                spoken_text = (
                    response_text if client.config.kids_mode_enabled else self._speech_friendly(response_text)
                )
                self._set_status(
                    "synthesizing",
                    "Generating speech",
                    response_preview=response_text[:240],
                )
                if client.config.kids_mode_enabled:
                    try:
                        self._set_status(
                            "speaking",
                            "Streaming ElevenLabs Flash speech",
                            tts_provider="elevenlabs-flash-stream",
                        )
                        interrupted = self._play_kids_stream(
                            client,
                            spoken_text,
                            barge_in=config.barge_in_enabled,
                            animate_motion=not preserve_ispy_guess_motion,
                        )
                    except (HermesBridgeError, httpx.HTTPError):
                        _LOGGER.warning(
                            "Kids low-latency speech stream failed; considering configured TTS fallback",
                            exc_info=True,
                        )
                        self._clear_streamed_audio()
                        if (
                            not conversation_is_current()
                            or self.stop_event.is_set()
                            or self._turn_stop_requested()
                            or self._privacy_requested.is_set()
                            or self._effective_power_mode() in {"meeting", "sleep"}
                        ):
                            break
                        speech = client.synthesize(spoken_text)
                        if (
                            not conversation_is_current()
                            or self.stop_event.is_set()
                            or self._turn_stop_requested()
                            or self._privacy_requested.is_set()
                            or self._effective_power_mode() in {"meeting", "sleep"}
                        ):
                            break
                        self._set_status(
                            "speaking",
                            "Reachy is speaking with fallback audio",
                            tts_provider=speech.provider,
                        )
                        interrupted = self._play_response(
                            speech,
                            spoken_text,
                            barge_in=config.barge_in_enabled,
                            animate_motion=not preserve_ispy_guess_motion,
                        )
                else:
                    speech = client.synthesize(spoken_text)
                    if (
                        self._turn_stop_requested()
                        or self._privacy_requested.is_set()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                    ):
                        break
                    self._set_status("speaking", "Reachy is speaking", tts_provider=speech.provider)
                    interrupted = self._play_response(
                        speech,
                        spoken_text,
                        barge_in=config.barge_in_enabled,
                    )
                if (
                    not conversation_is_current()
                    or self._turn_stop_requested()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                if (
                    not interrupted
                    and client.config.kids_mode_enabled
                    and client.config.kids_activity == "ispy"
                    and client.last_kids_next_action == "prepare_robot_round"
                ):
                    interrupted = self._continue_ispy_reachy_turn(
                        client,
                        barge_in=config.barge_in_enabled,
                    )
                    if (
                        not conversation_is_current()
                        or self._turn_stop_requested()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                    ):
                        break
                with self._status_lock:
                    self._status.turns_completed += 1
                if interrupted:
                    first_turn = False
                    continue
                if not config.continuous_conversation:
                    break
        except HermesBridgeError:
            self._set_status("error", "Hermes bridge request failed", bridge_healthy=False)
            raise
        finally:
            client.close()

    def _record_utterance(
        self,
        config: AppConfig,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> EndpointResult:
        recorder = AdaptiveEndpointRecorder(
            initial_timeout=config.initial_speech_timeout_seconds,
            max_duration=config.max_utterance_seconds,
            end_silence=config.end_silence_seconds,
            minimum_rms=config.vad_min_rms,
            noise_multiplier=config.vad_noise_multiplier,
        )
        result = recorder.record(
            self._read_16k_frame,
            noise_floor=self._noise.value,
            should_stop=should_stop or self.stop_event.is_set,
        )
        _LOGGER.info(
            "Speech endpoint: reason=%s speech=%s duration=%.2fs threshold=%.4f",
            result.reason,
            result.speech_detected,
            result.samples.size / 16000.0,
            result.threshold,
        )
        return result

    def _read_16k_frame(self) -> np.ndarray | None:
        raw = self.robot.media.get_audio_sample()
        if raw is None:
            time.sleep(0.002)
            return None
        normalized = mono_float32(raw)
        return resample_linear(normalized, self._sample_rate, 16000)

    def _discard_audio(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            self._read_16k_frame()

    def _play_asset(self, name: str) -> None:
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            return
        path = self.assets / name
        if path.exists():
            self.robot.media.play_sound(str(path))

    def _signal_error(self) -> None:
        self._play_asset("error.wav")
        if self._motion is not None:
            self._motion.error()

    def _play_kids_stream(
        self,
        client: HermesBridgeClient,
        text: str,
        *,
        barge_in: bool = True,
        animate_motion: bool = True,
    ) -> bool:
        """Stream child PCM while retaining immediate stop/privacy/wake control."""

        def cancelled() -> bool:
            return (
                self.stop_event.is_set()
                or (
                    client.config.kids_mode_enabled
                    and not self._kids_session_is_current(client.config.kids_session_id)
                )
                or self._conversation_stop_requested.is_set()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            )

        if cancelled():
            return False
        if animate_motion and self._motion is not None:
            self._motion.speaking()
        if self._spotter is not None:
            self._spotter.reset()

        stream_queue: queue.Queue[bytes | Exception | object] = queue.Queue(maxsize=16)
        stream_done = object()
        stream_cancel = threading.Event()

        def put_stream_item(item: bytes | Exception | object) -> None:
            while not stream_cancel.is_set():
                try:
                    stream_queue.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def produce_stream() -> None:
            try:
                for chunk in client.iter_kids_speech(
                    text,
                    should_stop=lambda: stream_cancel.is_set() or cancelled(),
                ):
                    if stream_cancel.is_set() or cancelled():
                        break
                    put_stream_item(chunk)
            except Exception as exc:  # preserve the producer error for the conversation thread
                put_stream_item(exc)
            finally:
                put_stream_item(stream_done)

        producer = threading.Thread(
            target=produce_stream,
            name="reachy-kids-tts-stream",
            daemon=True,
        )
        producer.start()

        first_audio_at: float | None = None
        queued_samples = 0
        remainder = b""
        download_complete = False
        interrupted = False
        try:
            while not download_complete or (
                first_audio_at is not None
                and time.monotonic() < first_audio_at + queued_samples / self._output_sample_rate + 0.15
            ):
                if cancelled():
                    self._clear_streamed_audio()
                    return False

                if not download_complete:
                    try:
                        item = stream_queue.get(timeout=0.01)
                    except queue.Empty:
                        item = None
                    if item is stream_done:
                        download_complete = True
                    elif isinstance(item, Exception):
                        raise item
                    elif isinstance(item, bytes):
                        raw = remainder + item
                        usable = len(raw) - (len(raw) % 2)
                        remainder = raw[usable:]
                        if usable:
                            audio = np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
                            output = resample_linear(audio, 24000, self._output_sample_rate)
                            if output.size:
                                if first_audio_at is None:
                                    first_audio_at = time.monotonic()
                                queued_samples += int(output.size)
                                if queued_samples > self._output_sample_rate * 120:
                                    raise HermesBridgeError("Kids Mode streaming speech exceeded the playback limit")
                                self.robot.media.push_audio_sample(output)

                if barge_in and self._spotter is not None:
                    frame = self._read_16k_frame()
                    if frame is not None:
                        keyword = self._spotter.accept(frame, 16000)
                        if keyword:
                            interrupted = True
                            self._clear_streamed_audio()
                            self._spotter.reset()
                            with self._status_lock:
                                self._status.interruptions += 1
                            self._set_status("listening", "Response interrupted; listening")
                            if self._motion is not None:
                                self._motion.listening()
                            _LOGGER.info("Streaming playback interrupted by local wake phrase: %s", keyword)
                            return True
                elif download_complete:
                    time.sleep(0.02)

            if first_audio_at is None or queued_samples == 0:
                raise HermesBridgeError("Kids Mode streaming speech returned no playable audio")
            _LOGGER.info(
                "Kids streaming speech played %.2fs of PCM using %s",
                queued_samples / self._output_sample_rate,
                client.last_tts_provider,
            )
            return interrupted
        except Exception:
            self._clear_streamed_audio()
            raise
        finally:
            stream_cancel.set()
            producer.join(timeout=2.5)
            if producer.is_alive():
                _LOGGER.warning("Kids Mode TTS producer did not exit promptly after cancellation")

    def _play_response(
        self,
        speech: SpeechAudio,
        text: str,
        *,
        barge_in: bool = True,
        animate_motion: bool = True,
    ) -> bool:
        """Play a response and allow a local wake-phrase barge-in.

        Pipeline mode cannot safely use open-mic RMS detection because Reachy's
        speaker is audible to its microphone. Reusing the local wake spotter
        avoids self-interruption; Realtime mode provides natural semantic VAD.
        """
        suffix = speech.extension if speech.extension.startswith(".") else ".audio"
        with tempfile.NamedTemporaryFile(prefix="reachy-hermes-response-", suffix=suffix, delete=False) as output:
            output.write(speech.data)
            path = Path(output.name)
        interrupted = False
        try:
            if (
                self._turn_stop_requested()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            ):
                return False
            duration = self._audio_duration(path, fallback_text=text)
            self._set_status("speaking", "Hermes is speaking")
            if animate_motion and self._motion is not None:
                self._motion.speaking()
            if self._spotter is not None:
                self._spotter.reset()
            self.robot.media.play_sound(str(path))
            deadline = time.monotonic() + duration + 0.15
            while time.monotonic() < deadline and not self.stop_event.is_set():
                if self._turn_stop_requested() or self._effective_power_mode() in {"meeting", "sleep"}:
                    self._clear_streamed_audio()
                    break
                if not barge_in or self._spotter is None:
                    time.sleep(0.02)
                    continue
                frame = self._read_16k_frame()
                if frame is None:
                    continue
                keyword = self._spotter.accept(frame, 16000)
                if not keyword:
                    continue
                interrupted = True
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
                self._spotter.reset()
                with self._status_lock:
                    self._status.interruptions += 1
                self._set_status("listening", "Response interrupted; listening")
                if self._motion is not None:
                    self._motion.listening()
                _LOGGER.info("Playback interrupted by local wake phrase: %s", keyword)
                break
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return interrupted

    @staticmethod
    def _audio_duration(path: Path, *, fallback_text: str) -> float:
        try:
            from mutagen import File as MutagenFile

            media = MutagenFile(path)
            if media is not None and media.info is not None:
                return max(0.1, min(float(media.info.length), 120.0))
        except Exception:
            _LOGGER.debug("Could not inspect TTS duration", exc_info=True)
        return max(1.0, min(len(fallback_text) / 13.0 + 0.6, 45.0))

    @staticmethod
    def _speech_friendly(text: str) -> str:
        text = _MEDIA_TAG.sub("", text)
        text = _MARKDOWN.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or "I completed the request, but there is no spoken response."
