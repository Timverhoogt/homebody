"""Wake-to-speech runtime for Reachy Mini Hermes."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import numpy as np

from .agent_audit import AgentAuditLog
from .agent_session import AgentSessionMixin
from .announcements import Announcement as Announcement  # re-exported for existing imports
from .announcements import AnnouncementsMixin
from .audio import (
    NoiseFloor,
    mono_float32,
    resample_linear,
)
from .config import AppConfig, load_config
from .hermes_client import HermesBridgeClient
from .home_assistant import HermesHomeAssistantProvider, HomeAssistantBridge
from .kids_runtime import KidsModeMixin
from .manual_control import ManualControlMixin
from .motion import VoiceMotion
from .presence import PresenceObservation
from .proactive import ProactiveMixin
from .realtime_client import RealtimeBridgeSession
from .robot_tools import (
    ReachyRobotActions,
)
from .safety_gate import POWER_MODES as _POWER_MODES
from .safety_gate import (
    ROBOT_ACTION_POLICY,
    SafetyGate,
)
from .vision_runtime import VisionMixin
from .vision_runtime import doa_yaw_degrees as doa_yaw_degrees  # re-exported for existing imports
from .voice_ha import HomeAssistantVoiceMixin
from .voice_pipeline import PipelineVoiceMixin
from .voice_realtime import (  # re-exported for existing imports
    PowerModeToolCall as PowerModeToolCall,
)
from .voice_realtime import (
    RealtimePlayback as RealtimePlayback,
)
from .voice_realtime import RealtimeVoiceMixin
from .voice_realtime import (
    completed_camera_call_id as completed_camera_call_id,
)
from .voice_realtime import (
    completed_power_mode_call as completed_power_mode_call,
)
from .voice_realtime import (
    realtime_audio_item_id as realtime_audio_item_id,
)
from .voice_realtime import (
    realtime_response_id as realtime_response_id,
)
from .wakeword import WAKE_PROMPT as _WAKE_PROMPT
from .wakeword import HeyHermesSpotter, ensure_kws_model

_LOGGER = logging.getLogger(__name__)
_WAKE_PHRASES_TEXT = "Hey Hermes · Okay Nabu · Hey Reachy"


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


class _RuntimeSafetyProbe:
    """Read the runtime state that safety rules need, each value under its own existing lock."""

    def __init__(self, runtime: HermesVoiceRuntime) -> None:
        self._runtime = runtime

    def runtime_ready(self) -> bool:
        runtime = self._runtime
        return not runtime.stop_event.is_set() and runtime._control_ready.is_set() and runtime._audio_ready

    def power_mode(self) -> str:
        return self._runtime._effective_power_mode()

    def privacy_requested(self) -> bool:
        return self._runtime._privacy_requested.is_set()

    def motors_confirmed(self) -> bool:
        return self._runtime._motors_enabled is True

    def kids_engaged(self) -> bool:
        runtime = self._runtime
        with runtime._kids_lock:
            return runtime._kids_active or runtime._kids_locked

    def kids_session_active(self) -> bool:
        runtime = self._runtime
        with runtime._kids_lock:
            return runtime._kids_active

    def agent_profile_active(self) -> bool:
        runtime = self._runtime
        with runtime._agent_lock:
            return runtime._capability_profile == "agent"

    def camera_control_active(self) -> bool:
        runtime = self._runtime
        with runtime._camera_control_lock:
            return runtime._camera_control_session_live_locked()

    def announcement_active(self) -> bool:
        return self._runtime._announcement_active.is_set()

    def voice_activity_busy(self) -> bool:
        return self._runtime._voice_activity_lock.locked()

    def status(self) -> tuple[str, str]:
        runtime = self._runtime
        with runtime._status_lock:
            return runtime._status.state, runtime._status.last_error

    def face_tracking_active(self) -> bool:
        return self._runtime._face_tracking_active

    def actions_ready(self) -> bool:
        return self._runtime._actions is not None

    def robot_busy(self) -> bool:
        actions = self._runtime._actions
        return actions is not None and bool(actions.busy or actions.pending_count)


class HermesVoiceRuntime(
    KidsModeMixin,
    AnnouncementsMixin,
    ManualControlMixin,
    ProactiveMixin,
    AgentSessionMixin,
    VisionMixin,
    HomeAssistantVoiceMixin,
    RealtimeVoiceMixin,
    PipelineVoiceMixin,
):
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
        self._init_vision_state()
        self._init_manual_control_state()
        self._power_mode = "standby"
        self._meeting_until = 0.0
        self._recording = False
        self._playback_stopped_for_privacy = False
        self._voice_activity_lock = threading.Lock()
        self._init_announcement_state()
        self._voice_activity_generation = 0
        self._init_proactive_state()
        self._audio_ready = False
        self._runtime_started = False
        self._control_ready = threading.Event()
        self._home_assistant_bridge: HomeAssistantBridge | None = None
        self._home_assistant_error = ""
        self._init_kids_state()
        self._safety_gate = SafetyGate(_RuntimeSafetyProbe(self))
        self._init_agent_session_state()

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
            payload["camera_control_active"] = self._camera_control_session_live_locked()
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

    @property
    def control_ready(self) -> bool:
        """Return whether external controls may begin hardware transitions."""
        return self._control_ready.is_set()

    def _new_bridge_client(self, config: AppConfig) -> HermesBridgeClient:
        """Create a bridge client; feature mixins call this so tests can patch one name."""
        return HermesBridgeClient(config)

    def _new_realtime_session(self, *args: object, **kwargs: object) -> RealtimeBridgeSession:
        """Create a Realtime session; feature mixins call this so tests can patch one name."""
        return RealtimeBridgeSession(*args, **kwargs)  # type: ignore[arg-type]

    def _before_robot_action(self) -> None:
        self._safety_gate.require(ROBOT_ACTION_POLICY)
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

    def _clear_streamed_audio(self) -> None:
        """Flush Realtime appsrc output without stopping microphone capture."""
        audio = getattr(self.robot.media, "audio", None)
        clear = getattr(audio, "clear_player", None)
        if callable(clear):
            clear()

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
